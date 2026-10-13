"""Log panel: shows log records with a level filter. Records arrive through a queue (see logsetup)."""

from __future__ import annotations

import logging
import queue
from collections import deque
from datetime import datetime

import wx

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def format_record(record: logging.LogRecord) -> str:
    stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
    return f"{stamp} {record.levelname:7s} {record.getMessage()}"


class LogPanel(wx.Panel):
    def __init__(self, parent: wx.Window, max_lines: int = 5000, level: str = "INFO") -> None:
        super().__init__(parent)
        self._records: deque[tuple[int, str]] = deque(maxlen=max_lines)
        self.level = logging.getLevelName(level)
        header = wx.BoxSizer(wx.HORIZONTAL)
        header.Add(wx.StaticText(self, label="Log level:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.level_choice = wx.Choice(self, choices=list(LEVELS))
        self.level_choice.SetStringSelection(level)
        self.level_choice.Bind(wx.EVT_CHOICE, self._on_level)
        header.Add(self.level_choice, 0, wx.ALIGN_CENTER_VERTICAL)
        self.text = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2 | wx.HSCROLL | wx.TE_DONTWRAP)
        self.text.SetFont(wx.Font(wx.FontInfo(9).Family(wx.FONTFAMILY_TELETYPE)))
        # minimum: three lines stay visible at the smallest window size
        self.text.SetMinSize(wx.Size(self.FromDIP(300), self.text.GetCharHeight() * 3 + self.FromDIP(8)))
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(header, 0, wx.BOTTOM, 4)
        sizer.Add(self.text, 1, wx.EXPAND)
        self.SetSizer(sizer)

    def drain(self, source: "queue.Queue[logging.LogRecord]") -> int:
        """Move all records from the queue into the panel. Returns the number of records."""
        lines: list[str] = []
        count = 0
        while True:
            try:
                record = source.get_nowait()
            except queue.Empty:
                break
            count += 1
            line = format_record(record)
            self._records.append((record.levelno, line))
            if record.levelno >= self.level:
                lines.append(line)
        if lines:
            self.text.AppendText("\n".join(lines) + "\n")
        return count

    def _on_level(self, _event: wx.Event) -> None:
        self.set_level(self.level_choice.GetStringSelection())

    def set_level(self, level: str) -> None:
        self.level = logging.getLevelName(level)
        self.level_choice.SetStringSelection(level)
        self.text.SetValue("".join(line + "\n" for levelno, line in self._records if levelno >= self.level))
        self.text.ShowPosition(self.text.GetLastPosition())

    @property
    def line_count(self) -> int:
        return sum(1 for levelno, _ in self._records if levelno >= self.level)
