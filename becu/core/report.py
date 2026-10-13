"""Result report: one CSV row per reader."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

from .job import ReaderResult

COLUMNS = (
    "row", "address", "host", "port", "serial", "firmware", "config_before", "config_after",
    "state", "note", "error_class", "error", "attempts", "duration_s", "started", "finished",
)


def result_row(result: ReaderResult) -> dict[str, object]:
    before = result.info_before
    after = result.info_after
    info = result.info
    return {
        "row": result.target.row_number,
        "address": result.target.address,
        "host": result.target.host,
        "port": result.target.port,
        "serial": info.serial if info else "",
        "firmware": f"{info.firmware_id} {info.firmware_version}".strip() if info else "",
        "config_before": before.config_id or "" if before else "",
        "config_after": after.config_id or "" if after else "",
        "state": result.state.value,
        "note": result.note,
        "error_class": result.error_class.value if result.error_class else "",
        "error": result.error_message,
        "attempts": result.attempts,
        "duration_s": f"{result.duration_s:.1f}" if result.duration_s is not None else "",
        "started": result.started_at.isoformat(timespec="seconds") if result.started_at else "",
        "finished": result.finished_at.isoformat(timespec="seconds") if result.finished_at else "",
    }


def write_report(path: str | Path, results: Iterable[ReaderResult]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, delimiter=";")
        writer.writeheader()
        for result in results:
            writer.writerow(result_row(result))
    return path
