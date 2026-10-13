"""Scheduler: runs one job per reader in its own thread, with a ramped start and retries.

``max_parallel=None`` means one thread per reader (all at once); ``start_rate`` limits how many
jobs start per second. Counters are exact numbers per state (no time estimate).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Sequence

from .bec_file import BecFile
from .csv_import import ReaderTarget
from .job import FINAL_STATES, JobOptions, ReaderJob, ReaderResult, State
from .session_api import SessionFactory

logger = logging.getLogger("becu.scheduler")


@dataclass
class RunOptions:
    max_parallel: int | None = None      #: None = as many as there are readers
    start_rate: float = 10.0             #: job starts per second (ramp)
    retries: int = 1                     #: additional attempts for retriable errors
    retry_delay: float = 5.0             #: seconds between attempts
    job: JobOptions = field(default_factory=JobOptions)


@dataclass(frozen=True)
class Counters:
    total: int
    pending: int
    handshaking: int
    updating: int
    rebooting: int
    updated: int
    errors: int
    elapsed_s: float

    @property
    def finished(self) -> int:
        return self.updated + self.errors

    def as_dict(self) -> dict[str, int | float]:
        return {
            "total": self.total, "pending": self.pending, "handshaking": self.handshaking,
            "updating": self.updating, "rebooting": self.rebooting, "updated": self.updated,
            "errors": self.errors, "elapsed_s": round(self.elapsed_s, 1),
        }


@dataclass(frozen=True)
class JobEvent:
    target: ReaderTarget
    state: State
    message: str
    attempt: int
    timestamp: datetime


EventSink = Callable[[JobEvent], None]


class Scheduler:
    def __init__(self, targets: Sequence[ReaderTarget], bec: BecFile, options: RunOptions,
                 make_session: SessionFactory, on_event: EventSink | None = None) -> None:
        if options.max_parallel is not None and options.max_parallel < 1:
            raise ValueError("max_parallel must be >= 1 or None")
        if options.start_rate <= 0:
            raise ValueError("start_rate must be > 0")
        self.targets = list(targets)
        self.bec = bec
        self.options = options
        self.make_session = make_session
        self.on_event = on_event
        self._lock = threading.Lock()
        self._states: dict[str, State] = {t.address: State.PENDING for t in self.targets}
        self._attempts: dict[str, int] = {t.address: 0 for t in self.targets}
        self._results: dict[str, ReaderResult] = {t.address: ReaderResult(t) for t in self.targets}
        self._stop = threading.Event()
        self.finished = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._finished_at: float | None = None

    # ---- public API ----------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("scheduler already started")
        self._started_at = time.monotonic()
        self._thread = threading.Thread(target=self._dispatch, name="becu-dispatcher", daemon=True)
        self._thread.start()

    def run(self) -> list[ReaderResult]:
        """Blocking: start, wait for the end, return the results in target order."""
        self.start()
        self.join()
        return self.results()

    def stop(self) -> None:
        """Start no further readers. Running readers finish their current attempt."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def join(self, timeout: float | None = None) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def results(self) -> list[ReaderResult]:
        with self._lock:
            return [self._results[t.address] for t in self.targets]

    def counters(self) -> Counters:
        with self._lock:
            states = list(self._states.values())
        end = self._finished_at if self._finished_at is not None else time.monotonic()
        elapsed = end - self._started_at if self._started_at is not None else 0.0
        return Counters(
            total=len(states),
            pending=states.count(State.PENDING),
            handshaking=states.count(State.HANDSHAKING),
            updating=states.count(State.UPDATING),
            rebooting=states.count(State.REBOOTING),
            updated=states.count(State.UPDATED),
            errors=states.count(State.ERROR),
            elapsed_s=elapsed,
        )

    # ---- internals -----------------------------------------------------------------------------

    def _emit(self, target: ReaderTarget, state: State, message: str) -> None:
        with self._lock:
            self._states[target.address] = state
            attempt = self._attempts[target.address]
        if self.on_event is not None:
            try:
                self.on_event(JobEvent(target, state, message, attempt, datetime.now()))
            except Exception:  # noqa: BLE001 - a UI problem must not disturb the run
                logger.exception("event sink failed")

    def _run_job(self, target: ReaderTarget, attempt: int, delayed: list, delayed_lock: threading.Lock) -> None:
        job = ReaderJob(target, self.bec, self.options.job, self.make_session, self._emit)
        result = job.run(attempt)
        retry = (result.state == State.ERROR and result.error_class is not None
                 and attempt <= self.options.retries
                 and result.error_class in _retriable_classes() and not self._stop.is_set())
        with self._lock:
            self._results[target.address] = result
            if retry:
                self._states[target.address] = State.PENDING
        if retry:
            logger.info("%s: retry %d/%d in %.0f s (%s)", target.address, attempt, self.options.retries,
                        self.options.retry_delay, result.error_class.value)
            self._emit(target, State.PENDING, f"retry in {self.options.retry_delay:.0f} s ({result.error_class.value})")
            with delayed_lock:
                delayed.append((time.monotonic() + self.options.retry_delay, target, attempt + 1))

    def _dispatch(self) -> None:
        queue: deque[tuple[ReaderTarget, int]] = deque((t, 1) for t in self.targets)
        delayed: list[tuple[float, ReaderTarget, int]] = []
        delayed_lock = threading.Lock()
        running: dict[str, threading.Thread] = {}
        interval = 1.0 / self.options.start_rate
        tick = min(0.02, interval)
        next_start = 0.0
        next_reap = 0.0
        limit = self.options.max_parallel or max(1, len(self.targets))
        try:
            while True:
                now = time.monotonic()
                with delayed_lock:
                    due = [item for item in delayed if item[0] <= now]
                    for item in due:
                        delayed.remove(item)
                for item in due:
                    queue.append((item[1], item[2]))
                if now >= next_reap or not queue:
                    # scanning 1800 threads on every iteration slows the ramp down; reap 4 times a second
                    for address, thread in list(running.items()):
                        if not thread.is_alive():
                            del running[address]
                    next_reap = now + 0.25
                if self._stop.is_set():
                    queue.clear()
                    with delayed_lock:
                        delayed.clear()
                with delayed_lock:
                    pending_retries = len(delayed)
                if not queue and not running and pending_retries == 0:
                    break
                if queue and len(running) < limit and now >= next_start:
                    target, attempt = queue.popleft()
                    with self._lock:
                        self._attempts[target.address] = attempt
                    thread = threading.Thread(
                        target=self._run_job, args=(target, attempt, delayed, delayed_lock),
                        name=f"becu-{target.address}", daemon=True)
                    running[target.address] = thread
                    thread.start()
                    next_start = now + interval
                    continue
                time.sleep(tick)
        finally:
            with self._lock:
                for target in self.targets:
                    result = self._results[target.address]
                    if result.state not in FINAL_STATES and self._states[target.address] == State.PENDING:
                        result.note = "not started (stopped)"
            self._finished_at = time.monotonic()
            self.finished.set()


def _retriable_classes():
    from .errors import RETRIABLE
    return RETRIABLE
