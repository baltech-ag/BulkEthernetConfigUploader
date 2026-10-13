"""Interface between the job logic and a reader session.

``reader_session.py`` implements it with the BALTECH SDK; the tests implement it with a fake.
A session covers one TCP connection to one reader. Errors are raised as ``ReaderError``
subclasses (see ``errors.py``), never as SDK exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from .bec_file import BecFile


@dataclass(frozen=True)
class ReaderInfo:
    firmware: str                 #: full Sys.GetInfo string, e.g. "1096 NUANCE RD 2.40.03 04/01/22 12345678"
    firmware_id: str              #: "1096"
    firmware_version: str         #: "2.40.03"
    serial: str                   #: "12345678"
    config_id: str | None         #: "99900-0001-0000-01" or None when the reader has no configuration
    config_name: str | None

    @classmethod
    def from_strings(cls, firmware: str, config_id: str | None, config_name: str | None) -> "ReaderInfo":
        parts = firmware.split()
        firmware_id = parts[0] if parts else ""
        serial = parts[-1] if len(parts) >= 2 and parts[-1].isdigit() else ""
        version = ""
        for p in parts[1:]:
            if p.count(".") == 2 and p.replace(".", "").isdigit():
                version = p
                break
        return cls(firmware, firmware_id, version, serial, config_id, config_name)


BlockCallback = Callable[[int, int], None]   #: (blocks done, blocks total)


class Session(Protocol):
    """One connection to one reader. ``open_plain`` or ``open_pki`` must be called first."""

    address: str

    def open_plain(self) -> None: ...

    def open_pki(self) -> None: ...

    def read_info(self) -> ReaderInfo: ...

    def upload(self, bec: BecFile, on_block: BlockCallback | None = None) -> None:
        """Send the configuration and finish with a reboot. Returns when the reboot command was accepted."""

    def factory_reset(self) -> None:
        """Sys.FactoryReset with reboot."""

    def close(self) -> None: ...

    def __enter__(self) -> "Session": ...

    def __exit__(self, *exc: object) -> None: ...


SessionFactory = Callable[[str], Session]   #: address "host:port" -> unopened session
