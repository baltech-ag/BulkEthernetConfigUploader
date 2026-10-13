"""Error taxonomy of the tool.

Input errors (file formats) and per-reader errors are separated. Per-reader errors carry an
``ErrorClass`` that the scheduler uses for the retry decision and that the report shows.
"""

from __future__ import annotations

from enum import StrEnum


class InputError(ValueError):
    """A file or a setting given by the user is not usable."""


class CsvFormatError(InputError):
    pass


class BecFormatError(InputError):
    pass


class PkiPackageError(InputError):
    pass


class ErrorClass(StrEnum):
    UNREACHABLE = "unreachable"          # no TCP connection (timeout, refused, no route)
    BUSY = "busy"                        # reader accepted and dropped the connection (another host is connected)
    PKI_FAILED = "pki_failed"            # handshake failed: wrong certificate chain, locked reader, crypto error
    ACCESS_DENIED = "access_denied"      # command refused by the access conditions of the security level
    CONFIG_CONFLICT = "config_conflict"  # reader refused the configuration (security code, version, customer key)
    UPLOAD_FAILED = "upload_failed"      # communication error or timeout during the upload
    REBOOT_TIMEOUT = "reboot_timeout"    # reader did not answer again after the reboot
    VERIFY_MISMATCH = "verify_mismatch"  # reader answered after the reboot with another configuration
    INTERNAL = "internal"                # unexpected exception in the tool


#: error classes that a retry can fix without operator action
RETRIABLE = frozenset({
    ErrorClass.UNREACHABLE,
    ErrorClass.BUSY,
    ErrorClass.UPLOAD_FAILED,
    ErrorClass.REBOOT_TIMEOUT,
})


class ReaderError(Exception):
    """Base of all per-reader errors. ``error_class`` drives retries and the report."""

    error_class: ErrorClass = ErrorClass.INTERNAL

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause

    @property
    def retriable(self) -> bool:
        return self.error_class in RETRIABLE


class UnreachableError(ReaderError):
    error_class = ErrorClass.UNREACHABLE


class BusyError(ReaderError):
    error_class = ErrorClass.BUSY


class PkiError(ReaderError):
    error_class = ErrorClass.PKI_FAILED


class AccessDeniedError(ReaderError):
    error_class = ErrorClass.ACCESS_DENIED


class ConfigConflictError(ReaderError):
    error_class = ErrorClass.CONFIG_CONFLICT


class UploadError(ReaderError):
    error_class = ErrorClass.UPLOAD_FAILED


class RebootTimeoutError(ReaderError):
    error_class = ErrorClass.REBOOT_TIMEOUT


class VerifyMismatchError(ReaderError):
    error_class = ErrorClass.VERIFY_MISMATCH
