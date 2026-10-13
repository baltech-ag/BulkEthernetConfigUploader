"""One simulated Kofax Ethernet card reader (firmware 1096 2.40.03): BRP over TCP, PKI secure channel, legacy BEC upload.

Behaviour copied from a real reader (measured 2026-10-13) and from the PKI protocol sequence of the
host side in brp_lib: one TCP session at a time, level 0 allows only the information commands, the
handshake takes about 21 s, a block upload about 16 ms per byte, a reboot about 5 s.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from enum import StrEnum

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import Encoding

from .brp import (
    PKI_ERR_CERT, PKI_ERR_CRYPTO, PKI_ERR_SECLEVEL_UNSUPPORTED, PKI_ERR_SEQCTR, PKI_ERR_SESSION_TIMEOUT,
    STATUS_ACCESS_DENIED, STATUS_INVALID_COMMAND, STATUS_OK, CommandFrame, FrameError, build_response,
    read_command,
)
from .crypto import (
    aes_cbc_decrypt, aes_cbc_encrypt, auth_cipher_params, cbc_mac, ecdh, generate_key, load_public_key_der,
    public_key_der, seq_iv, verify_signature,
)
from .testpki import TestPki

logger = logging.getLogger("mock.reader")

# command codes (device code << 8 | command code)
SYS_GET_BUFFER_SIZE = 0x0000
SYS_RESET = 0x0003
SYS_GET_INFO = 0x0004
SYS_GET_BOOT_STATUS = 0x0005
SYS_CFG_LOAD_BLOCK = 0x0016
SYS_CFG_GET_ID = 0x0020
SYS_GET_FEATURES = 0x0024
SYS_CFG_LOAD_PREPARE = 0x0026
SYS_CFG_LOAD_FINISH = 0x0027
SYS_FACTORY_RESET = 0x0028
PKI_PFS_GEN_KEY = 0x0901
PKI_PFS_AUTH_HOST_CERT = 0x0902
PKI_PFS_AUTH_RDR_CERT = 0x0903
PKI_TUNNEL2 = 0x0904

#: commands the reader answers without a PKI session (security level 0)
LEVEL0_ALLOWED = frozenset({SYS_GET_BUFFER_SIZE, SYS_GET_INFO, SYS_GET_BOOT_STATUS, SYS_CFG_GET_ID,
                            SYS_GET_FEATURES, PKI_PFS_GEN_KEY, PKI_PFS_AUTH_HOST_CERT, PKI_PFS_AUTH_RDR_CERT,
                            PKI_TUNNEL2})

SYS_ERR_CFG_NOT_FOUND = 0x03
SYS_ERR_CFG_CONFIG_SECURITY_CODE = 0x0A
CFG_LOAD_CANCEL, CFG_LOAD_FINISH, CFG_LOAD_FINISH_WITH_REBOOT = 0, 1, 2
IDLE_TIMEOUT_S = 300.0
BUFFER_SIZE = 600
FEATURE_IDS = (0x0000, 0x0001, 0x0002, 0x0035)   # a few feature ids; the tool does not evaluate them
MAX_FEATURE_ID = 0x0043


class Fault(StrEnum):
    NONE = "none"
    UPTODATE = "uptodate"          #: already carries the target configuration
    UNREACHABLE = "unreachable"    #: no listener (connection refused)
    BUSY = "busy"                  #: closes every connection at once (another host is connected)
    LOCKED = "locked"              #: trusts another root: the host certificate is rejected
    DENIED = "denied"              #: CfgLoadPrepare refused with ERR_ACCESS_DENIED
    CONFLICT = "conflict"          #: CfgLoadBlock refused with ErrCfgConfigSecurityCode
    DROP = "drop"                  #: closes the connection during the first upload, works on the retry
    MISMATCH = "mismatch"          #: reboots with the old configuration
    SLOW = "slow"                  #: handshake takes twice as long


#: what the tool should report per fault (state, error class) with retries >= 1 and no factory reset
EXPECTED = {
    Fault.NONE: ("updated", ""),
    Fault.UPTODATE: ("updated", ""),
    Fault.UNREACHABLE: ("error", "unreachable"),
    Fault.BUSY: ("error", "busy"),
    Fault.LOCKED: ("error", "pki_failed"),
    Fault.DENIED: ("error", "access_denied"),
    Fault.CONFLICT: ("error", "config_conflict"),
    Fault.DROP: ("updated", ""),
    Fault.MISMATCH: ("error", "verify_mismatch"),
    Fault.SLOW: ("updated", ""),
}


@dataclass
class Timing:
    scale: float = 1.0
    pfs_gen_key: float = 7.0
    pfs_auth_host_cert: float = 5.0     #: per certificate
    pfs_auth_rdr_cert: float = 4.0
    per_block_byte: float = 0.016
    reboot: float = 4.9
    factory_reset: float = 5.5

    def seconds(self, value: float) -> float:
        return value * self.scale


@dataclass
class Session:
    auth_key: bytes
    auth_iv: bytes
    chain: list[x509.Certificate]
    level: int | None = None
    session_key: bytes | None = None
    last_seq: int = 0


@dataclass
class Stats:
    connections: int = 0
    rejected_busy: int = 0
    rejected_rebooting: int = 0
    handshakes: int = 0
    uploads: int = 0
    reboots: int = 0
    factory_resets: int = 0
    status_errors: int = 0
    frame_errors: int = 0


class _Drop(Exception):
    """Close the connection without a response (simulated connection loss)."""


class MockReader:
    def __init__(self, index: int, address: str, serial: str, pki: TestPki, target: tuple[str, str],
                 initial: tuple[str, str] | None, fault: Fault = Fault.NONE, timing: Timing | None = None) -> None:
        self.index = index
        self.address = address
        self.serial = serial
        self.fault = fault
        self.timing = timing or Timing()
        self.target = target
        self.factory_config = initial
        self.config: tuple[str, str] | None = target if fault == Fault.UPTODATE else initial
        self.firmware = f"1096 NUANCE RD 2.40.03 04/01/22 {serial}"
        self.key = generate_key()
        self.cert = pki.make_reader_cert(serial, self.key)
        self.cert_der = self.cert.public_bytes(Encoding.DER)
        # a locked reader trusts a foreign root, so the real host chain does not verify
        self.root_cert = TestPki.generate().root_cert if fault == Fault.LOCKED else pki.root_cert
        self.stats = Stats()
        self.session: Session | None = None
        self.connection_active = False
        self.rebooting_until = 0.0
        self.prepared = False
        self.blocks: list[bytes] = []
        self.dropped_once = False
        #: DENIED/CONFLICT refuse every upload until a factory reset (tests may lower this to 1)
        self.upload_refusals_left = 10**9 if fault in (Fault.DENIED, Fault.CONFLICT) else 0

    # ---- connection -------------------------------------------------------------------------------

    def is_rebooting(self) -> bool:
        return time.monotonic() < self.rebooting_until

    @staticmethod
    def _reject(writer: asyncio.StreamWriter) -> None:
        """Close like the real reader does when it cannot serve: abortively (RST), so the host sees a read error."""
        sock = writer.get_extra_info("socket")
        if sock is not None:
            import socket
            import struct
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        writer.close()

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.is_rebooting():
            self.stats.rejected_rebooting += 1
            self._reject(writer)
            return
        if self.connection_active or self.fault == Fault.BUSY:
            self.stats.rejected_busy += 1
            self._reject(writer)
            return
        self.connection_active = True
        self.stats.connections += 1
        try:
            while True:
                try:
                    cmd = await asyncio.wait_for(read_command(reader), IDLE_TIMEOUT_S)
                except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
                    break
                except FrameError as e:
                    self.stats.frame_errors += 1
                    logger.warning("%s: %s", self.address, e)
                    break
                try:
                    status, payload, close_after = await self.execute(cmd, tunneled=False)
                except _Drop:
                    break
                writer.write(build_response(cmd.cmd_code, status, payload))
                try:
                    await writer.drain()
                except ConnectionError:
                    break
                if close_after:
                    break
        finally:
            self.connection_active = False
            writer.close()

    # ---- dispatch -----------------------------------------------------------------------------------

    async def execute(self, cmd: CommandFrame, tunneled: bool) -> tuple[int, bytes, bool]:
        """Returns (status, payload, close_after_response)."""
        if not tunneled and cmd.cmd_code not in LEVEL0_ALLOWED:
            return self._status(STATUS_ACCESS_DENIED)
        handler = self._handlers().get(cmd.cmd_code)
        if handler is None:
            return self._status(STATUS_INVALID_COMMAND)
        return await handler(cmd.params)

    def _handlers(self):
        return {
            SYS_GET_BUFFER_SIZE: self.sys_get_buffer_size,
            SYS_RESET: self.sys_reset,
            SYS_GET_INFO: self.sys_get_info,
            SYS_GET_BOOT_STATUS: self.sys_get_boot_status,
            SYS_CFG_LOAD_BLOCK: self.sys_cfg_load_block,
            SYS_CFG_GET_ID: self.sys_cfg_get_id,
            SYS_GET_FEATURES: self.sys_get_features,
            SYS_CFG_LOAD_PREPARE: self.sys_cfg_load_prepare,
            SYS_CFG_LOAD_FINISH: self.sys_cfg_load_finish,
            SYS_FACTORY_RESET: self.sys_factory_reset,
            PKI_PFS_GEN_KEY: self.pki_pfs_gen_key,
            PKI_PFS_AUTH_HOST_CERT: self.pki_pfs_auth_host_cert,
            PKI_PFS_AUTH_RDR_CERT: self.pki_pfs_auth_rdr_cert,
            PKI_TUNNEL2: self.pki_tunnel2,
        }

    def _status(self, status: int) -> tuple[int, bytes, bool]:
        if status != STATUS_OK:
            self.stats.status_errors += 1
        return status, b"", False

    async def _sleep(self, seconds: float) -> None:
        delay = self.timing.seconds(seconds)
        if delay > 0:
            await asyncio.sleep(delay)

    def _schedule_reboot(self, seconds: float) -> None:
        self.rebooting_until = time.monotonic() + self.timing.seconds(seconds)
        self.session = None
        self.prepared = False
        self.blocks = []
        self.stats.reboots += 1

    # ---- Sys commands -----------------------------------------------------------------------------

    async def sys_get_buffer_size(self, params: bytes) -> tuple[int, bytes, bool]:
        return STATUS_OK, BUFFER_SIZE.to_bytes(2, "big") * 3, False

    async def sys_get_info(self, params: bytes) -> tuple[int, bytes, bool]:
        return STATUS_OK, self.firmware.encode("ascii") + b"\x00", False

    async def sys_get_boot_status(self, params: bytes) -> tuple[int, bytes, bool]:
        return STATUS_OK, b"\x00\x00\x00\x00", False

    async def sys_cfg_get_id(self, params: bytes) -> tuple[int, bytes, bool]:
        if self.config is None:
            return self._status(SYS_ERR_CFG_NOT_FOUND)
        config_id, name = self.config
        payload = bytes([len(config_id)]) + config_id.encode("ascii") + bytes([len(name)]) + name.encode("ascii")
        return STATUS_OK, payload, False

    async def sys_get_features(self, params: bytes) -> tuple[int, bytes, bool]:
        payload = bytes([len(FEATURE_IDS)]) + b"".join(f.to_bytes(2, "big") for f in FEATURE_IDS)
        return STATUS_OK, payload + MAX_FEATURE_ID.to_bytes(2, "big"), False

    async def sys_cfg_load_prepare(self, params: bytes) -> tuple[int, bytes, bool]:
        if self.fault == Fault.DENIED and self.upload_refusals_left:
            self.upload_refusals_left -= 1
            return self._status(STATUS_ACCESS_DENIED)
        self.prepared = True
        self.blocks = []
        return STATUS_OK, b"", False

    async def sys_cfg_load_block(self, params: bytes) -> tuple[int, bytes, bool]:
        if len(params) < 2 or len(params) != 2 + params[1]:
            return self._status(STATUS_INVALID_COMMAND)
        data = params[2:]
        if self.fault == Fault.CONFLICT and self.upload_refusals_left:
            self.upload_refusals_left -= 1
            return self._status(SYS_ERR_CFG_CONFIG_SECURITY_CODE)
        if self.fault == Fault.DROP and not self.dropped_once and len(self.blocks) >= 1:
            self.dropped_once = True
            raise _Drop()
        await self._sleep(len(data) * self.timing.per_block_byte)
        self.blocks.append(data)
        return STATUS_OK, b"", False

    async def sys_cfg_load_finish(self, params: bytes) -> tuple[int, bytes, bool]:
        action = params[0] if params else CFG_LOAD_CANCEL
        if action == CFG_LOAD_CANCEL:
            self.prepared = False
            self.blocks = []
            return STATUS_OK, b"", False
        self._apply_uploaded_config()
        if action == CFG_LOAD_FINISH_WITH_REBOOT:
            self._schedule_reboot(self.timing.reboot)
            return STATUS_OK, b"", True
        return STATUS_OK, b"", False

    def _apply_uploaded_config(self) -> None:
        self.stats.uploads += 1
        self.prepared = False
        self.blocks = []
        if self.fault != Fault.MISMATCH:
            self.config = self.target

    async def sys_reset(self, params: bytes) -> tuple[int, bytes, bool]:
        self._schedule_reboot(self.timing.reboot)
        return STATUS_OK, b"", True

    async def sys_factory_reset(self, params: bytes) -> tuple[int, bytes, bool]:
        self.stats.factory_resets += 1
        self.config = self.factory_config
        self.upload_refusals_left = 0      # the conflict is gone after a factory reset
        self._schedule_reboot(self.timing.factory_reset)
        return STATUS_OK, b"", True

    # ---- PKI commands -----------------------------------------------------------------------------

    async def pki_pfs_gen_key(self, params: bytes) -> tuple[int, bytes, bool]:
        if len(params) < 2 or len(params) != 2 + int.from_bytes(params[:2], "big"):
            return self._status(STATUS_INVALID_COMMAND)
        try:
            host_tmp_public = load_public_key_der(params[2:])
        except ValueError:
            return self._status(PKI_ERR_CRYPTO)
        await self._sleep(self.timing.pfs_gen_key * (2 if self.fault == Fault.SLOW else 1))
        tmp_key = generate_key()
        auth_key, auth_iv = auth_cipher_params(ecdh(tmp_key, host_tmp_public))
        self.session = Session(auth_key, auth_iv, [self.root_cert])
        rdr_public = public_key_der(tmp_key)
        return STATUS_OK, len(rdr_public).to_bytes(2, "big") + rdr_public, False

    async def pki_pfs_auth_host_cert(self, params: bytes) -> tuple[int, bytes, bool]:
        session = self.session
        if session is None or session.session_key is not None:
            return self._status(PKI_ERR_CRYPTO)
        if len(params) < 2 or len(params) != 2 + int.from_bytes(params[:2], "big"):
            return self._status(STATUS_INVALID_COMMAND)
        try:
            plain = aes_cbc_decrypt(session.auth_key, session.auth_iv, params[2:])
        except ValueError:
            return self._status(PKI_ERR_CRYPTO)
        if len(plain) < 8:
            return self._status(PKI_ERR_CRYPTO)
        level = plain[0]
        is_end = plain[5] != 0
        cert_len = int.from_bytes(plain[6:8], "big")
        cert_der = plain[8:8 + cert_len]
        if len(cert_der) != cert_len or any(plain[8 + cert_len:]):
            return self._status(STATUS_INVALID_COMMAND)
        if level != 1:
            return self._status(PKI_ERR_SECLEVEL_UNSUPPORTED)
        if session.level is None:
            session.level = level
        elif session.level != level:
            return self._status(PKI_ERR_CRYPTO)
        try:
            cert = x509.load_der_x509_certificate(cert_der)
        except ValueError:
            return self._status(PKI_ERR_CERT)
        await self._sleep(self.timing.pfs_auth_host_cert * (2 if self.fault == Fault.SLOW else 1))
        issuer = session.chain[-1]
        if cert.issuer != issuer.subject:
            return self._status(PKI_ERR_CERT)
        try:
            verify_signature(issuer.public_key(), cert)
        except InvalidSignature:
            return self._status(PKI_ERR_CERT)
        session.chain.append(cert)
        if is_end:
            shared_2 = ecdh(self.key, cert.public_key())[:16]
            session.session_key = aes_cbc_encrypt(session.auth_key, session.auth_iv, shared_2)
            session.last_seq = 0
            self.stats.handshakes += 1
        return STATUS_OK, b"", False

    async def pki_pfs_auth_rdr_cert(self, params: bytes) -> tuple[int, bytes, bool]:
        session = self.session
        if session is None or session.level is None:
            return self._status(PKI_ERR_CRYPTO)
        await self._sleep(self.timing.pfs_auth_rdr_cert * (2 if self.fault == Fault.SLOW else 1))
        plain = len(self.cert_der).to_bytes(2, "big") + self.cert_der
        encrypted = aes_cbc_encrypt(session.auth_key, session.auth_iv, plain)
        return STATUS_OK, len(encrypted).to_bytes(2, "big") + encrypted, False

    async def pki_tunnel2(self, params: bytes) -> tuple[int, bytes, bool]:
        session = self.session
        if session is None or session.session_key is None:
            return self._status(PKI_ERR_SESSION_TIMEOUT)
        if len(params) < 22:
            return self._status(STATUS_INVALID_COMMAND)
        seq = int.from_bytes(params[0:4], "big")
        cmd_mac = params[4:20]
        enc_len = int.from_bytes(params[20:22], "big")
        encrypted = params[22:]
        if len(encrypted) != enc_len:
            return self._status(STATUS_INVALID_COMMAND)
        if seq <= session.last_seq:
            return self._status(PKI_ERR_SEQCTR)
        session.last_seq = seq
        key = session.session_key
        if cbc_mac(key, seq_iv(0x20, seq), encrypted) != cmd_mac:
            return self._status(PKI_ERR_CRYPTO)
        try:
            plain = aes_cbc_decrypt(key, seq_iv(0x10, seq), encrypted)
        except ValueError:
            return self._status(PKI_ERR_CRYPTO)
        if len(plain) < 4:
            return self._status(PKI_ERR_CRYPTO)
        inner_code = (plain[0] << 8) | plain[1]
        inner_len = int.from_bytes(plain[2:4], "big")
        inner_params = plain[4:4 + inner_len]
        if len(inner_params) != inner_len or any(plain[4 + inner_len:]):
            return self._status(STATUS_INVALID_COMMAND)
        inner = CommandFrame(0, inner_code, inner_params)
        status, payload, close_after = await self.execute(inner, tunneled=True)
        plain_resp = bytes([status]) + len(payload).to_bytes(2, "big") + payload
        enc_resp = aes_cbc_encrypt(key, seq_iv(0x40, seq), plain_resp)
        resp_mac = cbc_mac(key, seq_iv(0x80, seq), enc_resp)
        return STATUS_OK, resp_mac + len(enc_resp).to_bytes(2, "big") + enc_resp, close_after
