"""Virtual list of the readers that ended with an error."""

from __future__ import annotations

from typing import Sequence

import wx

from becu.core.job import ReaderResult

COLUMNS = (("Address", 170), ("Serial", 90), ("Error", 120), ("Message", 520), ("Attempts", 70))


class ErrorList(wx.ListCtrl):
    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, style=wx.LC_REPORT | wx.LC_VIRTUAL | wx.LC_SINGLE_SEL)
        for i, (name, width) in enumerate(COLUMNS):
            self.InsertColumn(i, name, width=width)
        # minimum: header and two rows stay visible at the smallest window size
        self.SetMinSize(wx.Size(self.FromDIP(300), self.GetCharHeight() * 4))
        self._rows: list[tuple[str, ...]] = []

    def set_results(self, results: Sequence[ReaderResult]) -> None:
        rows = [(
            r.target.address,
            r.info.serial if r.info else "",
            r.error_class.value if r.error_class else "",
            r.error_message,
            str(r.attempts),
        ) for r in results]
        if rows != self._rows:
            self._rows = rows
            self.SetItemCount(len(rows))
            self.Refresh()

    @property
    def count(self) -> int:
        return len(self._rows)

    def OnGetItemText(self, item: int, col: int) -> str:  # noqa: N802 - wx callback name
        return self._rows[item][col]
