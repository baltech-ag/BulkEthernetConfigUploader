"""Rough duration estimate for a run, shown before the start (GUI) and in the CLI summary.

The constants are the measured figures of the target readers (firmware 1096 2.40.03): PKI handshake
about 21 s, upload about 16 ms per configuration byte, reboot about 5 s, plus a few seconds for the
information reads and the verification. They serve the estimate only; the run itself does not use them.
"""

from __future__ import annotations

import math

HANDSHAKE_S = 21.0
UPLOAD_S_PER_BYTE = 0.016
REBOOT_S = 5.0
CHECKS_S = 4.0


def per_reader_seconds(payload_bytes: int) -> float:
    """Estimated seconds for one reader: handshake, upload of the configuration, reboot and checks."""
    return HANDSHAKE_S + UPLOAD_S_PER_BYTE * payload_bytes + REBOOT_S + CHECKS_S


def estimate_seconds(readers: int, start_rate: float, max_parallel: int | None, payload_bytes: int) -> float:
    """Estimated seconds for the whole run without retries or errors.

    Readers start at ``start_rate`` per second. With a limit on parallel readers, the readers beyond the
    limit start when others finish, so the run takes about one reader duration per batch of ``max_parallel``.
    """
    if readers <= 0:
        return 0.0
    batch = readers if max_parallel is None else min(max_parallel, readers)
    ramp = (batch - 1) / start_rate
    return ramp + per_reader_seconds(payload_bytes) * math.ceil(readers / batch)


def format_duration(seconds: float) -> str:
    """'about 45 s', 'about 2 min', 'about 1 h 5 min'; rounded up because it is an estimate."""
    if seconds < 60:
        return f"about {max(5, math.ceil(seconds / 5) * 5)} s"
    minutes = math.ceil(seconds / 60)
    if minutes < 60:
        return f"about {minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"about {hours} h {minutes} min" if minutes else f"about {hours} h"


def describe(readers: int, start_rate: float, max_parallel: int | None, payload_bytes: int) -> str:
    """One line with the estimate and its assumptions, for the GUI tooltip and the CLI summary."""
    total = format_duration(estimate_seconds(readers, start_rate, max_parallel, payload_bytes))
    limit = "" if max_parallel is None or max_parallel >= readers else f", at most {max_parallel} at a time"
    return (f"{total} for {readers} readers started at {start_rate:g} per second{limit}, about "
            f"{per_reader_seconds(payload_bytes):.0f} s per reader (PKI handshake {HANDSHAKE_S:.0f} s, upload "
            f"{UPLOAD_S_PER_BYTE * 1000:.0f} ms per byte for {payload_bytes} bytes, reboot {REBOOT_S:.0f} s, "
            f"checks {CHECKS_S:.0f} s); retries and errors not included")
