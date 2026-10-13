"""GUI entry point: ``python -m becu.gui``."""

from __future__ import annotations

import logging
import sys
import traceback

from becu.core.logsetup import setup_logging
from becu.core.pki_package import project_root

logger = logging.getLogger("becu.gui")


def main(argv: list[str] | None = None) -> int:
    log_file = setup_logging(project_root() / "logs", "INFO", console_level=None)
    import wx

    from .main_frame import MainFrame

    app = wx.App(False)

    def excepthook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        logger.error("unhandled error:\n%s", text)
        wx.MessageBox(f"Unexpected error:\n{exc}\n\nDetails are in {log_file}", "BulkEthernetConfigUploader",
                      wx.OK | wx.ICON_ERROR)

    sys.excepthook = excepthook
    frame = MainFrame()
    frame.Show()
    app.MainLoop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
