"""BRP frames as brp_lib 3.19 sends them over TCP (reader side).

Command frame: ``flags(1) cmd_code(2, big endian) len_lo len_hi params``. brp_lib always sets the
flags DEVCODE (0x04) and LEN16 (0x08); no checksum is used on TCP.
Response frame: ``flags(1) cmd_code(2) len_lo len_hi body``; with the STATUS flag (0x80) the body
is the one status byte, otherwise the response payload.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

FLAG_REPMODE = 0x01
FLAG_CONTMODE = 0x02
FLAG_DEVCODE = 0x04
FLAG_LEN16 = 0x08
FLAG_CHKSUM_MASK = 0x30
FLAG_STATUS = 0x80

STATUS_OK = 0x00
STATUS_INVALID_COMMAND = 0x41     # general: unknown command
STATUS_ACCESS_DENIED = 0x42       # general: refused by the access conditions of the security level

# Pki command status codes
PKI_ERR_CRYPTO = 0x01
PKI_ERR_TUNNEL = 0x02
PKI_ERR_CERT = 0x03
PKI_ERR_SEQCTR = 0x04
PKI_ERR_SECLEVEL_UNSUPPORTED = 0x05
PKI_ERR_SESSION_TIMEOUT = 0x06


class FrameError(Exception):
    pass


@dataclass(frozen=True)
class CommandFrame:
    flags: int
    cmd_code: int          #: (device code << 8) | command code
    params: bytes

    @property
    def device_code(self) -> int:
        return self.cmd_code >> 8

    @property
    def command_code(self) -> int:
        return self.cmd_code & 0xFF


async def read_command(reader: asyncio.StreamReader) -> CommandFrame:
    """Read one command frame. Raises ``asyncio.IncompleteReadError`` when the host closed the connection."""
    flags = (await reader.readexactly(1))[0]
    if not flags & FLAG_DEVCODE or flags & FLAG_CHKSUM_MASK:
        raise FrameError(f"unsupported frame flags 0x{flags:02X}")
    cmd_code = int.from_bytes(await reader.readexactly(2), "big")
    length = (await reader.readexactly(1))[0]
    if flags & FLAG_LEN16:
        length |= (await reader.readexactly(1))[0] << 8
    params = await reader.readexactly(length) if length else b""
    return CommandFrame(flags, cmd_code, params)


def build_response(cmd_code: int, status: int = STATUS_OK, payload: bytes = b"") -> bytes:
    flags = FLAG_DEVCODE | FLAG_LEN16
    if status != STATUS_OK:
        flags |= FLAG_STATUS
        body = bytes([status])
    else:
        body = payload
    return bytes([flags]) + cmd_code.to_bytes(2, "big") + len(body).to_bytes(2, "little") + body
