"""Fake readers and sessions for the unit tests. No SDK, no network."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from becu.core.bec_file import BecBlock, BecFile
from becu.core.errors import (
    AccessDeniedError, BusyError, ConfigConflictError, PkiError, UnreachableError, UploadError,
)
from becu.core.session_api import BlockCallback, ReaderInfo

FIRMWARE = "1096 NUANCE RD 2.40.03 04/01/22 {serial}"


def make_bec(config_id: str = "99900-0001-0000-01", name: str = "Config A", blocks: int = 3) -> BecFile:
    return BecFile(path=None, file_version=2, config_id=config_id, config_name=name,  # type: ignore[arg-type]
                   customer_key_signature=None,
                   blocks=tuple(BecBlock(2 if i == 0 else 0, bytes([i] * 10)) for i in range(blocks)))


@dataclass
class FakeReader:
    """State and scripted behaviour of one simulated reader."""

    serial: str = "12345678"
    config_id: str | None = "99900-0002-0000-01"
    config_name: str | None = "Config B"
    reachable: bool = True
    busy: bool = False
    pki_ok: bool = True
    upload_error: Exception | None = None       #: raised by upload() once, then cleared
    upload_error_count: int = 1                 #: how many uploads fail before success
    boots_to_config: str | None = None          #: config id after the reboot; None = the uploaded one
    reboot_duration: float = 0.0                #: seconds the reader is unreachable after the reboot command
    handshake_duration: float = 0.0
    factory_reset_clears: bool = True
    # bookkeeping
    uploads: int = 0
    factory_resets: int = 0
    plain_opens: int = 0
    pki_opens: int = 0
    unreachable_until: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def info(self) -> ReaderInfo:
        return ReaderInfo.from_strings(FIRMWARE.format(serial=self.serial), self.config_id, self.config_name)


class FakeSession:
    def __init__(self, address: str, reader: FakeReader) -> None:
        self.address = address
        self.reader = reader
        self.mode: str | None = None

    def _check_reachable(self) -> None:
        r = self.reader
        if not r.reachable or time.monotonic() < r.unreachable_until:
            raise UnreachableError(f"{self.address}: connect timeout")
        if r.busy:
            raise BusyError(f"{self.address}: connection closed by the reader")

    def open_plain(self) -> None:
        self._check_reachable()
        with self.reader.lock:
            self.reader.plain_opens += 1
        self.mode = "plain"

    def open_pki(self) -> None:
        self._check_reachable()
        with self.reader.lock:
            self.reader.pki_opens += 1
        if self.reader.handshake_duration:
            time.sleep(self.reader.handshake_duration)
        if not self.reader.pki_ok:
            raise PkiError(f"{self.address}: host certificate rejected")
        self.mode = "pki"

    def read_info(self) -> ReaderInfo:
        assert self.mode is not None, "session not open"
        return self.reader.info()

    def upload(self, bec: BecFile, on_block: BlockCallback | None = None) -> None:
        assert self.mode == "pki", "upload needs a PKI session"
        r = self.reader
        with r.lock:
            r.uploads += 1
            if r.upload_error is not None and r.upload_error_count > 0:
                r.upload_error_count -= 1
                raise r.upload_error
        for i in range(bec.block_count):
            if on_block:
                on_block(i + 1, bec.block_count)
        with r.lock:
            r.config_id = r.boots_to_config or bec.config_id
            r.config_name = bec.config_name
            r.unreachable_until = time.monotonic() + r.reboot_duration

    def factory_reset(self) -> None:
        assert self.mode == "pki"
        with self.reader.lock:
            self.reader.factory_resets += 1
            if self.reader.factory_reset_clears:
                self.reader.upload_error = None
            self.reader.config_id, self.reader.config_name = None, None
            self.reader.unreachable_until = time.monotonic() + self.reader.reboot_duration

    def close(self) -> None:
        self.mode = None

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class FakeFleet:
    """address -> FakeReader registry that acts as session factory."""

    def __init__(self) -> None:
        self.readers: dict[str, FakeReader] = {}

    def add(self, address: str, **kwargs) -> FakeReader:
        reader = FakeReader(**kwargs)
        self.readers[address] = reader
        return reader

    def __call__(self, address: str) -> FakeSession:
        return FakeSession(address, self.readers[address])


__all__ = ["FakeFleet", "FakeReader", "FakeSession", "make_bec", "AccessDeniedError", "ConfigConflictError",
           "UploadError"]
