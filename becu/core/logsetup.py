"""Logging: rotating file log, optional console log, and a queue handler for a UI."""

from __future__ import annotations

import logging
import logging.handlers
import queue
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s"
FILE_NAME = "becu.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5


def setup_logging(log_dir: str | Path, level: str = "INFO", console_level: str | None = "WARNING") -> Path:
    """Configure the root logger. Returns the log file path. Safe to call more than once."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / FILE_NAME
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_becu", False):
            root.removeHandler(handler)
            handler.close()
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
    file_handler.setLevel(level.upper())
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    file_handler._becu = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)
    root.setLevel(min(logging.getLevelName(level.upper()), logging.INFO))
    if console_level is not None:
        console = logging.StreamHandler()
        console.setLevel(console_level.upper())
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        console._becu = True  # type: ignore[attr-defined]
        root.addHandler(console)
        root.setLevel(min(root.level, logging.getLevelName(console_level.upper())))
    return log_file


def attach_queue_handler(target_queue: "queue.Queue[logging.LogRecord]", level: str = "INFO") -> logging.Handler:
    """Feed log records into a queue (for the GUI log panel). Returns the handler for removal."""
    handler = logging.handlers.QueueHandler(target_queue)
    handler.setLevel(level.upper())
    handler._becu = True  # type: ignore[attr-defined]
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(min(root.level, logging.getLevelName(level.upper())))
    return handler
