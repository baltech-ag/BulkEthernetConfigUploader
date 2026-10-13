"""Per-reader job: the state machine that updates one reader.

States follow the UI counters: PENDING, HANDSHAKING, UPDATING, REBOOTING, UPDATED, ERROR.
One ``ReaderJob.run()`` is one attempt; the scheduler decides about retries.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Callable

from .bec_file import BecFile
from .csv_import import ReaderTarget
from .errors import (
    AccessDeniedError, BusyError, ConfigConflictError, ErrorClass, ReaderError,
    RebootTimeoutError, UnreachableError, UploadError, VerifyMismatchError,
)
from .session_api import ReaderInfo, Session, SessionFactory

logger = logging.getLogger("becu.job")


class State(StrEnum):
    PENDING = "pending"
    HANDSHAKING = "handshaking"
    UPDATING = "updating"
    REBOOTING = "rebooting"
    UPDATED = "updated"
    ERROR = "error"


FINAL_STATES = frozenset({State.UPDATED, State.ERROR})


@dataclass
class JobOptions:
    plain_access: bool = True            #: pre-scan and verification without certificate (D2)
    factory_reset_on_failure: bool = False  #: D8: factory reset + second upload on access denied / config conflict
    reboot_wait: float = 3.0             #: seconds before the first poll after the reboot command
    reboot_timeout: float = 60.0         #: seconds until a reader that does not answer again counts as error
    reboot_poll: float = 0.5             #: seconds between polls


@dataclass
class ReaderResult:
    target: ReaderTarget
    state: State = State.PENDING
    note: str = ""
    error_class: ErrorClass | None = None
    error_message: str = ""
    attempts: int = 0
    info_before: ReaderInfo | None = None
    info_after: ReaderInfo | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    durations: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.state == State.UPDATED

    @property
    def duration_s(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def info(self) -> ReaderInfo | None:
        return self.info_after or self.info_before


StateCallback = Callable[[ReaderTarget, State, str], None]


class ReaderJob:
    """Updates one reader. ``run()`` never raises for reader problems; it returns an ERROR result."""

    def __init__(self, target: ReaderTarget, bec: BecFile, options: JobOptions,
                 make_session: SessionFactory, on_state: StateCallback | None = None) -> None:
        self.target = target
        self.bec = bec
        self.options = options
        self.make_session = make_session
        self.on_state = on_state
        self.result = ReaderResult(target)

    # ---- helpers ------------------------------------------------------------------------------

    def _set(self, state: State, message: str = "") -> None:
        self.result.state = state
        logger.info("%s: %s%s", self.target.address, state.value, f" ({message})" if message else "")
        if self.on_state is not None:
            self.on_state(self.target, state, message)

    def _timed(self, name: str, fn: Callable[[], object]) -> object:
        t0 = time.perf_counter()
        try:
            return fn()
        finally:
            self.result.durations[name] = round(time.perf_counter() - t0, 3)

    def _read_plain(self) -> ReaderInfo:
        with self.make_session(self.target.address) as session:
            session.open_plain()
            return session.read_info()

    def _wait_for_reader(self) -> ReaderInfo:
        """After a reboot command: poll until the reader answers again, or raise RebootTimeoutError.

        A reader that still answers with the old configuration has not rebooted yet; polling continues.
        """
        time.sleep(self.options.reboot_wait)
        deadline = time.monotonic() + self.options.reboot_timeout
        last_info: ReaderInfo | None = None
        last_error: ReaderError | None = None
        while time.monotonic() < deadline:
            try:
                info = self._read_plain()
            except (UnreachableError, BusyError, UploadError) as e:
                last_error = e      # not reachable, connection dropped or garbled while booting: poll again
            else:
                last_info = info
                if self.bec.matches(info.config_id):
                    return info
            time.sleep(self.options.reboot_poll)
        if last_info is not None:
            raise VerifyMismatchError(
                f"reader reports configuration {last_info.config_id!r} ({last_info.config_name!r}) "
                f"instead of {self.bec.config_id!r} after the reboot")
        raise RebootTimeoutError(
            f"no answer within {self.options.reboot_timeout:.0f} s after the reboot"
            + (f" (last error: {last_error})" if last_error else ""), cause=last_error)

    def _upload_in_session(self, session: Session) -> None:
        total = self.bec.block_count

        def on_block(done: int, _total: int) -> None:
            logger.debug("%s: block %d/%d", self.target.address, done, total)

        self._timed("upload", lambda: session.upload(self.bec, on_block))

    def _authenticate_and_upload(self) -> None:
        """PKI session + upload. Handles the optional factory-reset-and-retry path."""
        with self.make_session(self.target.address) as session:
            self._timed("handshake", session.open_pki)
            self._set(State.UPDATING, f"{self.bec.block_count} blocks")
            try:
                self._upload_in_session(session)
                return
            except (AccessDeniedError, ConfigConflictError) as e:
                if not self.options.factory_reset_on_failure:
                    raise
                logger.warning("%s: upload refused (%s); factory reset and second upload", self.target.address, e)
                session.factory_reset()
        # the reader reboots after the factory reset; wait, then start a new PKI session
        self.result.note = "after factory reset"
        self._set(State.REBOOTING, "factory reset")
        if self.options.plain_access:
            self._wait_until_answering()
        else:
            time.sleep(self.options.reboot_wait + 5.0)
        self._set(State.HANDSHAKING, "second attempt")
        with self.make_session(self.target.address) as session:
            self._timed("handshake2", session.open_pki)
            self._set(State.UPDATING, f"{self.bec.block_count} blocks (after factory reset)")
            self._upload_in_session(session)

    def _wait_until_answering(self) -> None:
        time.sleep(self.options.reboot_wait)
        deadline = time.monotonic() + self.options.reboot_timeout
        while time.monotonic() < deadline:
            try:
                self._read_plain()
                return
            except (UnreachableError, BusyError, UploadError):
                time.sleep(self.options.reboot_poll)
        raise RebootTimeoutError(f"no answer within {self.options.reboot_timeout:.0f} s after the factory reset")

    # ---- main sequence -----------------------------------------------------------------------

    def run(self, attempt: int = 1) -> ReaderResult:
        result = self.result
        result.attempts = attempt
        result.started_at = datetime.now()
        result.error_class = None
        result.error_message = ""
        try:
            self._set(State.HANDSHAKING, f"attempt {attempt}")
            if self.options.plain_access:
                result.info_before = self._timed("precheck", self._read_plain)
                if self.bec.matches(result.info_before.config_id):
                    result.info_after = result.info_before
                    result.note = "already up to date"
                    self._set(State.UPDATED, result.note)
                    return result
            self._authenticate_and_upload()
            self._set(State.REBOOTING)
            if self.options.plain_access:
                result.info_after = self._timed("reboot", self._wait_for_reader)
            else:
                time.sleep(self.options.reboot_wait)
                result.note = (result.note + "; " if result.note else "") + "not verified"
            info = result.info
            logger.info("%s: updated%s: serial %s, firmware %s %s, configuration %s -> %s, phases %s",
                        self.target.address, f" ({result.note})" if result.note else "",
                        info.serial if info else "?", info.firmware_id if info else "?",
                        info.firmware_version if info else "?",
                        result.info_before.config_id if result.info_before else "?",
                        result.info_after.config_id if result.info_after else "?", result.durations)
            self._set(State.UPDATED, result.note)
        except ReaderError as e:
            result.error_class = e.error_class
            result.error_message = str(e)
            logger.warning("%s: %s: %s", self.target.address, e.error_class.value, e)
            self._set(State.ERROR, f"{e.error_class.value}: {e}")
        except Exception as e:  # noqa: BLE001 - a bug must not kill the worker thread silently
            result.error_class = ErrorClass.INTERNAL
            result.error_message = f"{type(e).__name__}: {e}"
            logger.exception("%s: unexpected error", self.target.address)
            self._set(State.ERROR, result.error_message)
        finally:
            result.finished_at = datetime.now()
        return result
