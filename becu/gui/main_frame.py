"""Main window: inputs, run control with hard counters, errors list, log panel.

All reader communication runs in the scheduler's threads. The window polls the scheduler with a
timer; no wx call is made from a worker thread.
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

import wx

from becu import __version__
from becu.core.bec_file import BecFile, load_bec
from becu.core.csv_import import (
    DEFAULT_PORT, CsvTable, ReaderTarget, TargetSet, build_targets, column_has_ports, load_csv, suggest_ip_column,
)
from becu.core.errors import InputError
from becu.core.estimate import describe as describe_estimate, estimate_seconds, format_duration
from becu.core.job import JobOptions, ReaderResult, State
from becu.core.logsetup import attach_queue_handler
from becu.core.pki_package import PkiPackage, find_default_package, load_pki_package, project_root
from becu.core.report import write_report
from becu.core.scheduler import Counters, RunOptions, Scheduler
from becu.core.session_api import SessionFactory
from .error_list import ErrorList
from .log_panel import LogPanel

logger = logging.getLogger("becu.gui")

TITLE = "BulkEthernetConfigUploader"
TIMER_MS = 200
NONE_CHOICE = "(none)"
ESTIMATE_NONE = "Estimated duration: -"
ESTIMATE_HINT = ("Rough duration of the whole update from the reader count, the start rate, the parallel limit "
                 "and the configuration size. Retries and errors are not included.")
COUNTER_STATES = (
    (State.PENDING, "pending"), (State.HANDSHAKING, "handshaking"), (State.UPDATING, "updating"),
    (State.REBOOTING, "rebooting"), (State.UPDATED, "updated"), (State.ERROR, "errors"),
)

FactoryBuilder = Callable[[Path], SessionFactory]


def _sdk_factory_builder(cert_path: Path) -> SessionFactory:
    from becu.core.reader_session import make_sdk_session_factory
    return make_sdk_session_factory(cert_path)


def quote_path(path: Path | str) -> str:
    """A path argument for the command line, always quoted (Windows file names cannot contain quotes)."""
    return f'"{path}"'


class MainFrame(wx.Frame):
    def __init__(self, factory_builder: FactoryBuilder | None = None,
                 interactive: bool = True, report_dir: Path | None = None) -> None:
        # 860 px high where the screen allows it; the work area of a 768 px screen (about 720 px) limits it
        height = min(860, wx.GetClientDisplayRect().height)
        super().__init__(None, title=f"{TITLE} {__version__}", size=(1000, height))
        self.factory_builder = factory_builder or _sdk_factory_builder
        self.interactive = interactive
        self.report_dir = report_dir or project_root() / "reports"
        self.log_dir = project_root() / "logs"
        self.table: CsvTable | None = None
        self.bec: BecFile | None = None
        self.package: PkiPackage | None = None
        self.scheduler: Scheduler | None = None
        self.results: dict[str, ReaderResult] = {}
        self.report_path: Path | None = None
        self.run_open = False   # a started run whose finish the timer has not handled yet
        self.last_error = ""
        self.log_queue: queue.Queue[logging.LogRecord] = queue.Queue()
        self._log_handler = attach_queue_handler(self.log_queue, "DEBUG")

        self._build_ui()
        self.CreateStatusBar()
        # The window cannot shrink below the point where the errors list or the log would vanish.
        self.SetMinClientSize(self.panel.GetSizer().GetMinSize())
        minimum, size = self.GetMinSize(), self.GetSize()
        self.SetSize(max(size.width, minimum.width), max(size.height, minimum.height))
        self._load_default_certificate()
        self._refresh_derived()
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_timer, self.timer)
        self.timer.Start(TIMER_MS)
        self.Bind(wx.EVT_CLOSE, self._on_close)

    # ---- layout ------------------------------------------------------------------------------------

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        self.panel = panel
        root = wx.BoxSizer(wx.VERTICAL)
        center = wx.ALIGN_CENTER_VERTICAL
        # Compact rows: 4 px above and beside each row, 4 px once below the last row of a box.
        row_flags = wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP

        # Children of a wx.StaticBoxSizer must be created with its wx.StaticBox as parent.
        self.inputs_panel = wx.Panel(panel)
        inputs = wx.BoxSizer(wx.VERTICAL)

        # readers: file name and reader count on one row, the full path in the tooltip
        readers_box = wx.StaticBoxSizer(wx.VERTICAL, self.inputs_panel, "Readers")
        rb = readers_box.GetStaticBox()
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(rb, label="CSV file:", size=(90, -1)), 0, center)
        self.readers_label = self._info_label(rb, "No CSV file loaded.")
        row.Add(self.readers_label, 1, center | wx.RIGHT, 6)
        self.problems_button = wx.Button(rb, label="Skipped rows...")
        self.problems_button.Bind(wx.EVT_BUTTON, self._on_show_problems)
        self.problems_button.Disable()
        row.Add(self.problems_button, 0, wx.RIGHT, 6)
        self.csv_button = wx.Button(rb, label="Browse...")
        self.csv_button.Bind(wx.EVT_BUTTON, self._on_browse_csv)
        row.Add(self.csv_button, 0)
        readers_box.Add(row, 0, row_flags, 4)

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(rb, label="Address column:", size=(90, -1)), 0, center)
        self.ip_choice = wx.Choice(rb, size=(200, -1))
        self.ip_choice.SetToolTip("Column with the reader addresses: a.b.c.d or a.b.c.d:port")
        self.ip_choice.Bind(wx.EVT_CHOICE, self._on_column_change)
        row.Add(self.ip_choice, 0, center | wx.RIGHT, 18)
        self.port_label = wx.StaticText(rb, label=f"Port column (optional, otherwise {DEFAULT_PORT}):")
        row.Add(self.port_label, 0, center | wx.RIGHT, 6)
        self.port_choice = wx.Choice(rb, size=(200, -1))
        self.port_choice.Bind(wx.EVT_CHOICE, self._on_column_change)
        row.Add(self.port_choice, 0, center)
        readers_box.Add(row, 0, row_flags, 4)
        readers_box.AddSpacer(4)
        inputs.Add(readers_box, 0, wx.EXPAND | wx.BOTTOM, 4)

        # configuration and certificate: file name and details on one row each
        files_box = wx.StaticBoxSizer(wx.VERTICAL, self.inputs_panel, "Configuration and certificate")
        fb = files_box.GetStaticBox()
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(fb, label="BEC file:", size=(90, -1)), 0, center)
        self.bec_label = self._info_label(fb, "No configuration loaded.")
        row.Add(self.bec_label, 1, center | wx.RIGHT, 6)
        self.bec_button = wx.Button(fb, label="Browse...")
        self.bec_button.Bind(wx.EVT_BUTTON, self._on_browse_bec)
        row.Add(self.bec_button, 0)
        files_box.Add(row, 0, row_flags, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(fb, label="Certificate:", size=(90, -1)), 0, center)
        self.cert_label = self._info_label(fb, "No certificate package loaded.")
        row.Add(self.cert_label, 1, center | wx.RIGHT, 6)
        self.cert_button = wx.Button(fb, label="Change...")
        self.cert_button.Bind(wx.EVT_BUTTON, self._on_browse_cert)
        row.Add(self.cert_button, 0)
        files_box.Add(row, 0, row_flags, 4)
        files_box.AddSpacer(4)
        inputs.Add(files_box, 0, wx.EXPAND | wx.BOTTOM, 4)

        # options, with the equivalent command line as the last row
        options_box = wx.StaticBoxSizer(wx.VERTICAL, self.inputs_panel, "Options")
        ob = options_box.GetStaticBox()
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(ob, label="Max. parallel readers:"), 0, center | wx.RIGHT, 6)
        self.parallel_text = wx.TextCtrl(ob, value="all", size=(70, -1))
        self.parallel_text.SetToolTip("Readers in progress at the same time. 'all' = no limit.")
        row.Add(self.parallel_text, 0, center | wx.RIGHT, 18)
        row.Add(wx.StaticText(ob, label="Start up to"), 0, center | wx.RIGHT, 6)
        self.rate_spin = wx.SpinCtrl(ob, min=1, max=1000, initial=10, size=(70, -1))
        self.rate_spin.SetToolTip("Limits the connection burst at the start of a run. "
                                  "1000 starts all readers nearly at once.")
        row.Add(self.rate_spin, 0, center | wx.RIGHT, 6)
        row.Add(wx.StaticText(ob, label="readers per second"), 0, center | wx.RIGHT, 18)
        row.Add(wx.StaticText(ob, label="Automatic retries for temporary errors:"), 0, center | wx.RIGHT, 6)
        self.retries_spin = wx.SpinCtrl(ob, min=0, max=10, initial=1, size=(60, -1))
        self.retries_spin.SetToolTip(
            "Additional attempts, 5 seconds apart, when a reader is unreachable or busy, the connection is lost "
            "during the upload, or the reader does not answer after the reboot. A rejected certificate, access "
            "denied, a refused configuration or a wrong configuration after the reboot are not retried.")
        row.Add(self.retries_spin, 0, center)
        options_box.Add(row, 0, row_flags, 4)
        self.plain_check = wx.CheckBox(
            ob, label="Read reader information without certificate (skip up-to-date readers, verify after reboot)")
        self.plain_check.SetValue(True)
        options_box.Add(self.plain_check, 0, wx.LEFT | wx.RIGHT | wx.TOP, 4)
        self.factory_check = wx.CheckBox(
            ob, label="Factory reset and upload again when the reader refuses the configuration")
        options_box.Add(self.factory_check, 0, wx.LEFT | wx.RIGHT | wx.TOP, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(ob, label="Command line for automation:"), 0, center | wx.RIGHT, 6)
        self.command_text = wx.TextCtrl(ob, style=wx.TE_READONLY)
        self.command_text.SetToolTip("The same update as a command line call, for example from a script; run it in "
                                     "the program folder. --auto-confirm skips the confirmation prompt.")
        # gray text; an explicit foreground colour needs an explicit background, else the read-only field turns white
        self.command_text.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        self.command_text.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_BTNFACE))
        row.Add(self.command_text, 1, center | wx.RIGHT, 6)
        self.copy_button = wx.Button(ob, label="Copy")
        self.copy_button.Bind(wx.EVT_BUTTON, self._on_copy_command)
        row.Add(self.copy_button, 0)
        options_box.Add(row, 0, row_flags, 4)
        options_box.AddSpacer(4)
        inputs.Add(options_box, 0, wx.EXPAND)
        for control, event in ((self.parallel_text, wx.EVT_TEXT), (self.rate_spin, wx.EVT_SPINCTRL),
                               (self.retries_spin, wx.EVT_SPINCTRL), (self.plain_check, wx.EVT_CHECKBOX),
                               (self.factory_check, wx.EVT_CHECKBOX)):
            control.Bind(event, self._on_option_change)
        self.inputs_panel.SetSizer(inputs)
        root.Add(self.inputs_panel, 0, wx.EXPAND | wx.ALL, 6)

        # run control
        run_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "Run")
        runb = run_box.GetStaticBox()
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.start_button = wx.Button(runb, label="Start")
        self.start_button.Bind(wx.EVT_BUTTON, self._on_start)
        row.Add(self.start_button, 0, wx.RIGHT, 6)
        self.stop_button = wx.Button(runb, label="Stop")
        self.stop_button.Bind(wx.EVT_BUTTON, self._on_stop)
        self.stop_button.Disable()
        bold = self.start_button.GetFont().Bold()
        self.start_button.SetFont(bold)
        self.stop_button.SetFont(bold)
        row.Add(self.stop_button, 0, wx.RIGHT, 18)
        self.elapsed_label = wx.StaticText(runb, label="Elapsed 00:00")
        row.Add(self.elapsed_label, 0, center | wx.RIGHT, 18)
        self.estimate_label = wx.StaticText(runb, label=ESTIMATE_NONE)
        self.estimate_label.SetToolTip(ESTIMATE_HINT)
        row.Add(self.estimate_label, 0, center | wx.RIGHT, 18)
        self.gauge = wx.Gauge(runb, range=1)
        row.Add(self.gauge, 1, center)
        run_box.Add(row, 0, row_flags, 4)
        counters = wx.BoxSizer(wx.HORIZONTAL)
        self.counter_labels: dict[State, wx.StaticText] = {}
        big = wx.Font(wx.FontInfo(16).Bold())
        for state, caption in COUNTER_STATES:
            box = wx.BoxSizer(wx.VERTICAL)
            value = wx.StaticText(runb, label="0", style=wx.ALIGN_CENTER)
            value.SetFont(big)
            box.Add(value, 0, wx.ALIGN_CENTER)
            box.Add(wx.StaticText(runb, label=caption), 0, wx.ALIGN_CENTER)
            counters.Add(box, 1, wx.EXPAND)
            self.counter_labels[state] = value
        box = wx.BoxSizer(wx.VERTICAL)
        self.total_label = wx.StaticText(runb, label="0", style=wx.ALIGN_CENTER)
        self.total_label.SetFont(big)
        box.Add(self.total_label, 0, wx.ALIGN_CENTER)
        box.Add(wx.StaticText(runb, label="total"), 0, wx.ALIGN_CENTER)
        counters.Add(box, 1, wx.EXPAND)
        run_box.Add(counters, 0, wx.EXPAND | wx.ALL, 4)
        root.Add(run_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # errors
        errors_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "Errors")
        eb = errors_box.GetStaticBox()
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.retry_button = wx.Button(eb, label="Retry errors")
        self.retry_button.Bind(wx.EVT_BUTTON, self._on_retry)
        self.retry_button.Disable()
        row.Add(self.retry_button, 0, wx.RIGHT, 6)
        self.export_button = wx.Button(eb, label="Export report...")
        self.export_button.Bind(wx.EVT_BUTTON, self._on_export)
        self.export_button.Disable()
        row.Add(self.export_button, 0, wx.RIGHT, 6)
        self.logs_button = wx.Button(eb, label="Open log folder")
        self.logs_button.Bind(wx.EVT_BUTTON, lambda _e: self.open_log_folder())
        row.Add(self.logs_button, 0)
        errors_box.Add(row, 0, wx.LEFT | wx.RIGHT | wx.TOP, 4)
        self.error_list = ErrorList(eb)
        errors_box.Add(self.error_list, 1, wx.EXPAND | wx.ALL, 4)
        root.Add(errors_box, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # log
        log_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "Log")
        self.log_panel = LogPanel(log_box.GetStaticBox(), level="INFO")
        log_box.Add(self.log_panel, 1, wx.EXPAND | wx.ALL, 4)
        root.Add(log_box, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        panel.SetSizer(root)

    @staticmethod
    def _info_label(parent: wx.Window, text: str) -> wx.StaticText:
        """One-line label that shortens with '...' instead of widening the window or overlapping its neighbours."""
        label = wx.StaticText(parent, label=text, style=wx.ST_ELLIPSIZE_END | wx.ST_NO_AUTORESIZE)
        label.SetMinSize((100, -1))
        return label

    # ---- defaults ----------------------------------------------------------------------------------

    def _load_default_certificate(self) -> None:
        """The single zip in certs/ is preselected; everything else starts empty (no settings are stored)."""
        try:
            cert = find_default_package()
        except InputError as e:
            self.cert_label.SetLabel(str(e))
            return
        self.load_cert(cert)

    # ---- messages ----------------------------------------------------------------------------------

    def _error(self, message: str) -> None:
        self.last_error = message
        logger.error(message)
        self.SetStatusText(message)
        if self.interactive:
            wx.MessageBox(message, TITLE, wx.OK | wx.ICON_ERROR, self)

    def _confirm(self, message: str) -> bool:
        if not self.interactive:
            return True
        return wx.MessageBox(message, TITLE, wx.YES_NO | wx.ICON_QUESTION, self) == wx.YES

    # ---- inputs --------------------------------------------------------------------------------------

    def load_csv(self, path: Path) -> bool:
        """Load the reader list. A header row is detected automatically and never treated as a reader."""
        try:
            self.table = load_csv(path)
        except InputError as e:
            self.table = None
            self.readers_label.SetLabel(str(e))
            self.readers_label.UnsetToolTip()
            self.ip_choice.Clear()
            self.port_choice.Clear()
            self._error(str(e))
            return False
        self.ip_choice.Set(self.table.columns)
        index = suggest_ip_column(self.table)
        self.ip_choice.SetSelection(index if index is not None else 0)
        self.port_choice.Set([NONE_CHOICE] + self.table.columns)
        self.port_choice.SetSelection(0)
        self._refresh_readers()
        return True

    def _on_column_change(self, _event: wx.Event) -> None:
        self._refresh_readers()

    def _on_option_change(self, _event: wx.Event) -> None:
        self._refresh_derived()

    # ---- command line equivalent -----------------------------------------------------------------------

    def command_line(self) -> str:
        """The ``python -m becu.cli`` call that runs the update with the current settings (empty if incomplete).

        Columns are passed as 0-based indices (the column names stay a GUI detail), paths always in quotes.
        """
        ip_col = self.ip_column()
        if self.table is None or ip_col is None or self.bec is None or self.package is None:
            return ""
        if getattr(sys, "frozen", False):       # installed program: the CLI executable next to the GUI
            launcher = [quote_path(Path(sys.executable).with_name("becu-cli.exe"))]
        else:
            launcher = [r".venv\Scripts\python.exe", "-m", "becu.cli"]
        parts = launcher + ["--csv", quote_path(self.table.path), "--ip-column", str(ip_col)]
        port_col = self.port_column()
        if port_col is not None:
            parts += ["--port-column", str(port_col)]
        parts += ["--config", quote_path(self.bec.path), "--cert", quote_path(self.package.path),
                  "--max-parallel", self.parallel_text.GetValue().strip() or "all",
                  "--start-rate", str(self.rate_spin.GetValue()),
                  "--retries", str(self.retries_spin.GetValue())]
        if not self.plain_check.GetValue():
            parts.append("--no-plain-access")
        if self.factory_check.GetValue():
            parts.append("--factory-reset-on-failure")
        parts.append("--auto-confirm")
        return " ".join(parts)

    def _refresh_command(self) -> None:
        text = self.command_line()
        self.command_text.SetValue(text if text else "(select a CSV file, a configuration and a certificate)")
        self.copy_button.Enable(bool(text))

    def _refresh_derived(self) -> None:
        self._refresh_command()
        self._refresh_estimate()

    def _refresh_estimate(self) -> None:
        """Static estimate of the whole run, shown before the start; no time-left figure during the run."""
        target_set = self.build_target_set()
        text, hint = ESTIMATE_NONE, ESTIMATE_HINT
        if target_set is not None and target_set.targets and self.bec is not None:
            try:
                options = self.run_options()
            except InputError:
                options = None
            if options is not None:
                readers, size = len(target_set.targets), self.bec.payload_size
                text = "Estimated duration: " + format_duration(
                    estimate_seconds(readers, options.start_rate, options.max_parallel, size))
                hint = describe_estimate(readers, options.start_rate, options.max_parallel, size)
        if self.estimate_label.GetLabel() != text:
            self.estimate_label.SetLabel(text)
            self.panel.Layout()
        self.estimate_label.SetToolTip(hint)

    def _on_copy_command(self, _event: wx.Event) -> None:
        text = self.command_line()
        if text and wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(text))
            wx.TheClipboard.Close()
            self.SetStatusText("Command line copied to the clipboard.")

    def ip_column(self) -> int | None:
        sel = self.ip_choice.GetSelection()
        return None if self.table is None or sel == wx.NOT_FOUND else sel

    def port_column(self) -> int | None:
        sel = self.port_choice.GetSelection()
        if self.table is None or sel <= 0 or not self.port_choice.IsShown():
            return None
        return sel - 1

    def build_target_set(self) -> TargetSet | None:
        col = self.ip_column()
        if self.table is None or col is None:
            return None
        return build_targets(self.table, col, self.port_column(), DEFAULT_PORT)

    def _refresh_readers(self) -> None:
        col = self.ip_column()
        if self.table is None or col is None:
            return
        has_ports = column_has_ports(self.table, col)
        self.port_label.Show(not has_ports)
        self.port_choice.Show(not has_ports)
        self.inputs_panel.Layout()
        target_set = self.build_target_set()
        assert target_set is not None
        text = f"{self.table.path.name}: {len(target_set.targets)} readers"
        text += ", header row detected" if self.table.has_header else ", no header row"
        if target_set.problems:
            text += f", {len(target_set.problems)} rows skipped"
        if has_ports:
            text += " (ports taken from the address column)"
        self.readers_label.SetLabel(text)
        self.readers_label.SetToolTip(str(self.table.path))
        self.problems_button.Enable(bool(target_set.problems))
        self._refresh_derived()

    def _on_show_problems(self, _event: wx.Event) -> None:
        target_set = self.build_target_set()
        if target_set is None or not target_set.problems:
            return
        lines = [f"row {p.row_number}: {p.reason} ({p.raw!r})" for p in target_set.problems[:200]]
        if len(target_set.problems) > 200:
            lines.append(f"... {len(target_set.problems) - 200} more")
        wx.MessageBox("\n".join(lines), "Skipped rows", wx.OK | wx.ICON_INFORMATION, self)

    def load_bec(self, path: Path) -> bool:
        try:
            self.bec = load_bec(path)
        except InputError as e:
            self.bec = None
            self.bec_label.SetLabel(str(e))
            self.bec_label.UnsetToolTip()
            self._error(str(e))
            return False
        draft = " (DRAFT)" if self.bec.is_draft else ""
        self.bec_label.SetLabel(f"{path.name}: {self.bec.config_id} \"{self.bec.config_name}\"{draft}, "
                                f"{self.bec.block_count} blocks, {self.bec.payload_size} bytes")
        self.bec_label.SetToolTip(str(path))
        self._refresh_derived()
        return True

    def load_cert(self, path: Path) -> bool:
        try:
            self.package = load_pki_package(path)
        except InputError as e:
            self.package = None
            self.cert_label.SetLabel(str(e))
            self.cert_label.UnsetToolTip()
            self._error(str(e))
            return False
        self.cert_label.SetLabel(self.package.describe())
        self.cert_label.SetToolTip(str(path))
        self._refresh_derived()
        return True

    def _on_browse_csv(self, _event: wx.Event) -> None:
        with wx.FileDialog(self, "Reader list", wildcard="CSV files (*.csv;*.txt)|*.csv;*.txt|All files|*.*",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.load_csv(Path(dialog.GetPath()))

    def _on_browse_bec(self, _event: wx.Event) -> None:
        with wx.FileDialog(self, "Configuration", wildcard="BEC files (*.bec)|*.bec|All files|*.*",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.load_bec(Path(dialog.GetPath()))

    def _on_browse_cert(self, _event: wx.Event) -> None:
        with wx.FileDialog(self, "Certificate package", wildcard="Zip files (*.zip)|*.zip",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.load_cert(Path(dialog.GetPath()))

    # ---- run -----------------------------------------------------------------------------------------

    def run_options(self) -> RunOptions:
        text = self.parallel_text.GetValue().strip().lower()
        if text in ("", "all"):
            max_parallel = None
        elif text.isdigit() and int(text) >= 1:
            max_parallel = int(text)
        else:
            raise InputError("Max. parallel readers must be a positive number or 'all'")
        return RunOptions(
            max_parallel=max_parallel, start_rate=float(self.rate_spin.GetValue()),
            retries=self.retries_spin.GetValue(), retry_delay=5.0,
            job=JobOptions(plain_access=self.plain_check.GetValue(),
                           factory_reset_on_failure=self.factory_check.GetValue()))

    @property
    def running(self) -> bool:
        return self.scheduler is not None and not self.scheduler.finished.is_set()

    def start_run(self, targets: Sequence[ReaderTarget] | None = None, confirm: bool = True) -> bool:
        if self.running:
            return False
        if self.bec is None:
            self._error("Select a BEC configuration file first.")
            return False
        if self.package is None:
            self._error("No certificate package loaded.")
            return False
        if targets is None:
            target_set = self.build_target_set()
            if target_set is None or not target_set.targets:
                self._error("Load a CSV file with at least one valid reader address.")
                return False
            targets = target_set.targets
            self.results = {}
        try:
            options = self.run_options()
        except InputError as e:
            self._error(str(e))
            return False
        if confirm and not self._confirm(
                f"Update {len(targets)} readers with {self.bec.config_id} \"{self.bec.config_name}\"?"):
            return False
        factory = self.factory_builder(self.package.path)
        logger.info("run started: %d readers, configuration %s, certificate %s, max parallel %s, start rate %g/s",
                    len(targets), self.bec.config_id, self.package.path.name,
                    options.max_parallel or "all", options.start_rate)
        self.scheduler = Scheduler(targets, self.bec, options, factory)
        self.run_open = True
        self.report_path = None
        self.inputs_panel.Enable(False)
        self.start_button.Disable()
        self.stop_button.Enable()
        self.retry_button.Disable()
        self.export_button.Disable()
        self.gauge.SetRange(max(1, len(targets)))
        self.gauge.SetValue(0)
        self.SetStatusText("Running ...")
        self.scheduler.start()
        return True

    def stop_run(self) -> None:
        if self.scheduler is not None and self.running:
            self.scheduler.stop()
            self.stop_button.Disable()
            self.SetStatusText("Stopping: no further readers are started, running readers finish ...")

    def retry_errors(self, confirm: bool = True) -> bool:
        failed = [r.target for r in self.results.values() if r.state == State.ERROR]
        if not failed:
            return False
        return self.start_run(failed, confirm=confirm)

    def counters(self) -> Counters | None:
        return self.scheduler.counters() if self.scheduler is not None else None

    def _update_run_view(self) -> None:
        if self.scheduler is None:
            return
        c = self.scheduler.counters()
        for state, _caption in COUNTER_STATES:
            self.counter_labels[state].SetLabel(str(getattr(c, "errors" if state == State.ERROR else state.value)))
        self.total_label.SetLabel(str(c.total))
        self.gauge.SetRange(max(1, c.total))
        self.gauge.SetValue(min(c.finished, max(1, c.total)))
        minutes, seconds = divmod(int(c.elapsed_s), 60)
        self.elapsed_label.SetLabel(f"Elapsed {minutes:02d}:{seconds:02d}")
        for result in self.scheduler.results():
            self.results[result.target.address] = result
        self.error_list.set_results([r for r in self.results.values() if r.state == State.ERROR])

    def _on_timer(self, _event: wx.Event) -> None:
        self.log_panel.drain(self.log_queue)
        if self.scheduler is None:
            return
        self._update_run_view()
        # An explicit flag, not the state of the Start button: a modal dialog disables the whole frame, so
        # the button reported 'disabled' and every tick finalized the finished run again (log line + report).
        if self.run_open and self.scheduler.finished.is_set():
            self.run_open = False
            self._finish_run()

    def _finish_run(self) -> None:
        assert self.scheduler is not None
        c = self.scheduler.counters()
        self.inputs_panel.Enable(True)
        self.start_button.Enable()
        self.stop_button.Disable()
        self.retry_button.Enable(any(r.state == State.ERROR for r in self.results.values()))
        self.export_button.Enable()
        try:
            self.report_dir.mkdir(parents=True, exist_ok=True)
            self.report_path = write_report(
                self.report_dir / f"becu-{datetime.now():%Y%m%d-%H%M%S}.csv", list(self.results.values()))
        except OSError as e:
            self.report_path = None
            logger.error("report not written: %s", e)
        stopped = " (stopped)" if self.scheduler.stopping else ""
        summary = (f"Finished{stopped}: {c.updated} updated, {c.errors} errors, {c.pending} not started "
                   f"in {int(c.elapsed_s)} s")
        if self.report_path is not None:
            summary += f". Report: {self.report_path}"
        logger.info(summary)
        self.SetStatusText(summary)

    # ---- buttons -------------------------------------------------------------------------------------

    def _on_start(self, _event: wx.Event) -> None:
        self.start_run()

    def _on_stop(self, _event: wx.Event) -> None:
        self.stop_run()

    def _on_retry(self, _event: wx.Event) -> None:
        self.retry_errors()

    def _on_export(self, _event: wx.Event) -> None:
        with wx.FileDialog(self, "Export report", wildcard="CSV files (*.csv)|*.csv",
                           defaultFile=self.report_path.name if self.report_path else "becu-report.csv",
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.export_report(Path(dialog.GetPath()))

    def export_report(self, path: Path) -> Path:
        if self.report_path is not None and self.report_path.is_file():
            shutil.copyfile(self.report_path, path)
        else:
            write_report(path, list(self.results.values()))
        self.SetStatusText(f"Report exported: {path}")
        return path

    def open_log_folder(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(self.log_dir)  # type: ignore[attr-defined]

    def _on_close(self, event: wx.CloseEvent) -> None:
        if self.running:
            if not self._confirm("A run is active. Stop it and close the window?"):
                event.Veto()
                return
            self.scheduler.stop()  # type: ignore[union-attr]
        self.timer.Stop()
        logging.getLogger().removeHandler(self._log_handler)
        event.Skip()
