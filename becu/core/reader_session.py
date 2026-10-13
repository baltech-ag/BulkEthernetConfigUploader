"""Reader session on top of baltech-sdk 3.19.x (brp_lib with TCP transport and PKI secure channel).

Maps every SDK exception to a ``ReaderError`` so that the job logic and the report never see SDK
types. One ``SdkSession`` is one TCP connection; it is used from one worker thread only.
"""

from __future__ import annotations

import logging
import warnings
from contextlib import suppress
from pathlib import Path

with warnings.catch_warnings():
    # baltech-sdk 3.19.x docstrings contain "\." style sequences that Python 3.13 reports at compile time
    warnings.simplefilter("ignore", SyntaxWarning)
    from baltech_sdk import PKI, Brp, Tcp
    from baltech_sdk import errors as sdk
    from baltech_sdk.baltech_api import typedefs as api

from .bec_file import BecFile
from .errors import (
    AccessDeniedError, BusyError, ConfigConflictError, PkiError, ReaderError, UnreachableError,
    UploadError,
)
from .session_api import BlockCallback, ReaderInfo, SessionFactory

logger = logging.getLogger("becu.session")

_PKI_ERRORS: tuple[type[BaseException], ...] = tuple(
    getattr(sdk, name) for name in (
        "PkiOperationFailed", "HostCertificateFormatError", "HostCertificateInvalidSignatureError",
        "DeviceCertificateFormatError", "DeviceCertificateInvalidSignature", "SecurityLevelNotSupportedError",
        "SequenceCounterNotInSyncError", "InvalidHMACError", "CryptoFormatError", "CryptoRecvDevError",
        "InvalidKeyError", "GenRandomDataError",
    ) if hasattr(sdk, name))

_CONNECTION_LOST: tuple[type[BaseException], ...] = tuple(
    getattr(sdk, name) for name in ("ReadIOError", "WriteIOError", "WaitIOError", "CloseIOError") if hasattr(sdk, name))

#: device status codes of Sys.CfgLoad* that mean "this configuration does not fit this reader"
_CONFIG_CONFLICTS: tuple[type[BaseException], ...] = tuple(
    getattr(api, name) for name in (
        "Sys_ErrCfgConfigSecurityCode", "Sys_ErrCfgVersion", "Sys_ErrInvalidCfgBlock", "Sys_ErrCfgFull",
        "Sys_ErrCfgAccess", "Sys_ErrCfgAccessDenied", "Sys_ErrFactsetRestore",
    ) if hasattr(api, name))


def _text(e: BaseException) -> str:
    return f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}".strip(": ")


class SdkSession:
    def __init__(self, address: str, pki_zip: Path | None, session_timeout_ms: int = 0) -> None:
        self.address = address
        self.pki_zip = pki_zip
        self.session_timeout_ms = session_timeout_ms
        self._brp: Brp | None = None

    # ---- lifecycle -------------------------------------------------------------------------------

    def open_plain(self) -> None:
        brp = Brp(Tcp(self.address), monitor="disabled")
        self._open(brp, pki=False)

    def open_pki(self) -> None:
        if self.pki_zip is None:
            raise PkiError("no certificate package configured")
        crypto = PKI(self.pki_zip, session_timeout=self.session_timeout_ms)
        brp = Brp(Tcp(self.address), crypto=crypto, monitor="disabled")
        self._open(brp, pki=True)

    def _open(self, brp: Brp, pki: bool) -> None:
        try:
            brp.open()
        except sdk.OpenIOError as e:
            raise UnreachableError(f"no TCP connection to {self.address} ({_text(e)})", cause=e) from None
        except _CONNECTION_LOST as e:
            raise BusyError(f"{self.address} closed the connection; another host may be connected ({_text(e)})",
                            cause=e) from None
        except _PKI_ERRORS as e:
            raise PkiError(f"PKI handshake with {self.address} failed ({_text(e)})", cause=e) from None
        except sdk.CommandDeniedError as e:
            raise PkiError(f"{self.address} refuses the security level ({_text(e)})", cause=e) from None
        except sdk.ComTimeoutError as e:
            raise UnreachableError(f"{self.address} did not answer during the {'handshake' if pki else 'connect'} "
                                   f"({_text(e)})", cause=e) from None
        except sdk.StatusCodeError as e:
            kind = PkiError if pki else BusyError
            raise kind(f"opening {self.address} failed ({_text(e)})", cause=e) from None
        self._brp = brp

    def close(self) -> None:
        brp, self._brp = self._brp, None
        if brp is not None:
            with suppress(Exception):
                brp.close()

    def __enter__(self) -> "SdkSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def brp(self) -> Brp:
        if self._brp is None:
            raise RuntimeError("session is not open")
        return self._brp

    # ---- commands --------------------------------------------------------------------------------

    def read_info(self) -> ReaderInfo:
        try:
            firmware = self.brp.Sys_GetInfo()
            try:
                cfg = self.brp.Sys_CfgGetId()
                config_id, config_name = cfg.ConfigId, cfg.ConfigName
            except api.Sys_ErrCfgNotFound:
                config_id, config_name = None, None
        except ReaderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise self._map_comm_error(e, "reading the reader information") from None
        return ReaderInfo.from_strings(firmware, config_id, config_name)

    def upload(self, bec: BecFile, on_block: BlockCallback | None = None) -> None:
        brp = self.brp
        prepared = False
        try:
            try:
                brp.Sys_CfgLoadPrepare("Default")
                prepared = True
            except sdk.UnsupportedCommandError:
                logger.debug("%s: Sys.CfgLoadPrepare not supported, using blocks + reset", self.address)
            total = bec.block_count
            for i, block in enumerate(bec.blocks, start=1):
                brp.Sys_CfgLoadBlock(Version=block.version, Data=block.data)
                if on_block is not None:
                    on_block(i, total)
            if prepared:
                brp.Sys_CfgLoadFinish("FinalizeCfgLoadWithReboot")
            else:
                brp.Sys_Reset()
        except Exception as e:  # noqa: BLE001
            if prepared:
                with suppress(Exception):
                    brp.Sys_CfgLoadFinish("CancelCfgLoad")
            raise self._map_upload_error(e) from None

    def factory_reset(self) -> None:
        try:
            self.brp.Sys_FactoryReset(PerformReboot=True)
        except Exception as e:  # noqa: BLE001
            raise self._map_upload_error(e, what="factory reset") from None

    # ---- error mapping -----------------------------------------------------------------------------

    def _map_comm_error(self, e: BaseException, what: str) -> ReaderError:
        if isinstance(e, sdk.OpenIOError):
            return UnreachableError(f"{self.address}: {what} failed ({_text(e)})", cause=e)
        if isinstance(e, _CONNECTION_LOST):
            return BusyError(f"{self.address}: connection lost while {what} ({_text(e)})", cause=e)
        if isinstance(e, sdk.ComTimeoutError):
            return BusyError(f"{self.address}: no answer while {what} ({_text(e)})", cause=e)
        if isinstance(e, sdk.CommandDeniedError):
            return AccessDeniedError(f"{self.address}: access denied while {what} ({_text(e)})", cause=e)
        return UploadError(f"{self.address}: {what} failed ({_text(e)})", cause=e)

    def _map_upload_error(self, e: BaseException, what: str = "upload") -> ReaderError:
        if isinstance(e, sdk.CommandDeniedError):
            return AccessDeniedError(f"{self.address}: {what} refused, access denied at this security level "
                                     f"({_text(e)})", cause=e)
        if isinstance(e, _CONFIG_CONFLICTS):
            return ConfigConflictError(f"{self.address}: reader refused the configuration ({_text(e)})", cause=e)
        if isinstance(e, sdk.UnsupportedCommandError):
            return ConfigConflictError(f"{self.address}: command not supported by this firmware ({_text(e)})", cause=e)
        if isinstance(e, _PKI_ERRORS):
            return PkiError(f"{self.address}: secure session failed during {what} ({_text(e)})", cause=e)
        if isinstance(e, (sdk.OpenIOError, *_CONNECTION_LOST, sdk.ComTimeoutError)):
            return UploadError(f"{self.address}: connection problem during {what} ({_text(e)})", cause=e)
        if isinstance(e, api.BaltechApiError):
            # another reader status code: let the operator look, do not retry blindly
            return ConfigConflictError(f"{self.address}: reader reported {_text(e)} during {what}", cause=e)
        return UploadError(f"{self.address}: {what} failed ({_text(e)})", cause=e)


def make_sdk_session_factory(pki_zip: Path | None, session_timeout_ms: int = 0) -> SessionFactory:
    def factory(address: str) -> SdkSession:
        return SdkSession(address, pki_zip, session_timeout_ms)
    return factory
