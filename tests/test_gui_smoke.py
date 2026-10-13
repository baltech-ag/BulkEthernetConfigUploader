"""GUI smoke test: drives the main window without clicks, with the fake reader fleet."""

import time
from pathlib import Path

import pytest

wx = pytest.importorskip("wx")

from becu.core.job import State  # noqa: E402
from becu.gui.main_frame import MainFrame  # noqa: E402
from tests.fakes import FakeFleet  # noqa: E402
from tests.test_bec_file import SAMPLE  # noqa: E402
from tests.test_pki_package import make_zip  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return wx.App(False)


def pump(frame: MainFrame, until, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not until():
        wx.Yield()
        frame._on_timer(None)
        time.sleep(0.02)
        assert time.monotonic() < deadline, "timeout while waiting for the GUI"


def test_gui_run_with_fake_fleet(app, tmp_path: Path):
    csv_path = tmp_path / "readers.csv"
    csv_path.write_text("Site;IP\nA;10.0.0.1\nB;10.0.0.2\nC;10.0.0.3\nD;bad\n", encoding="utf-8")
    bec_path = tmp_path / "c.bec"
    bec_path.write_text(SAMPLE, encoding="utf-8")
    zip_path = make_zip(tmp_path / "certs.zip")

    fleet = FakeFleet()
    normal = fleet.add("10.0.0.1:2939", config_id="99900-0001-0000-01")   # normal update
    fleet.add("10.0.0.2:2939", config_id="99900-0002-0000-01")           # already up to date
    unreachable = fleet.add("10.0.0.3:2939", reachable=False)            # error, then retry succeeds

    frame = MainFrame(factory_builder=lambda cert: fleet, interactive=False, report_dir=tmp_path / "reports")
    try:
        assert frame.load_cert(zip_path)
        assert "security level 1" in frame.cert_label.GetLabel()
        assert frame.load_bec(bec_path)
        assert "Config B" in frame.bec_label.GetLabel()
        assert frame.load_csv(csv_path)
        assert frame.ip_choice.GetStringSelection() == "IP"
        assert frame.port_choice.IsShown()               # no ports in the address column
        assert frame.readers_label.GetLabel().startswith("readers.csv: 3 readers, header row detected, 1 rows skipped")
        assert frame.estimate_label.GetLabel() == "Estimated duration: about 35 s"   # 3 readers, 10 bytes
        frame.retries_spin.SetValue(0)
        frame.rate_spin.SetValue(1000)
        frame.plain_check.SetValue(False)
        frame._on_option_change(None)
        command = frame.command_line()
        assert command.startswith(r'.venv\Scripts\python.exe -m becu.cli --csv "')
        assert f'--csv "{csv_path}" --ip-column 1 --config "{bec_path}" --cert "{zip_path}" ' in command
        assert "--max-parallel all --start-rate 1000 --retries 0 --no-plain-access --auto-confirm" in command
        assert frame.command_text.GetValue() == command and frame.copy_button.IsEnabled()
        frame.plain_check.SetValue(True)
        frame._on_option_change(None)
        assert "--no-plain-access" not in frame.command_line()

        assert frame.start_run(confirm=False)
        assert not frame.start_button.IsEnabled()
        pump(frame, lambda: frame.start_button.IsEnabled())   # re-enabled by the final timer tick
        c = frame.counters()
        assert (c.total, c.updated, c.errors) == (3, 2, 1)
        assert normal.uploads == 1 and normal.config_id == "99900-0002-0000-01"
        assert frame.counter_labels[State.UPDATED].GetLabel() == "2"
        assert frame.counter_labels[State.ERROR].GetLabel() == "1"
        assert frame.error_list.count == 1 and frame.error_list.OnGetItemText(0, 0) == "10.0.0.3:2939"
        assert frame.retry_button.IsEnabled() and frame.export_button.IsEnabled()
        assert frame.report_path is not None and frame.report_path.is_file()
        assert frame.results["10.0.0.2:2939"].note == "already up to date"

        # A modal dialog disables the frame; the timer must not finalize the finished run again.
        finishes: list[int] = []
        original_finish = frame._finish_run
        frame._finish_run = lambda: (finishes.append(1), original_finish())
        frame.Disable()
        for _ in range(5):
            frame._on_timer(None)
        frame.Enable()
        frame._finish_run = original_finish
        assert not finishes

        unreachable.reachable = True
        assert frame.retry_errors(confirm=False)
        pump(frame, lambda: frame.start_button.IsEnabled())
        assert frame.counters().total == 1 and frame.counters().updated == 1
        assert frame.error_list.count == 0
        assert not frame.retry_button.IsEnabled()
        exported = frame.export_report(tmp_path / "export.csv")
        text = exported.read_text(encoding="utf-8-sig")
        assert text.count("\n") == 4 and "10.0.0.3:2939" in text and "updated" in text
        frame.log_panel.drain(frame.log_queue)
        assert frame.log_panel.line_count > 0
        frame.log_panel.set_level("ERROR")
        assert frame.log_panel.line_count <= frame.log_panel.text.GetNumberOfLines()
    finally:
        frame.timer.Stop()
        frame.Destroy()
        wx.Yield()


def test_gui_rejects_missing_inputs(app, tmp_path: Path):
    frame = MainFrame(factory_builder=lambda cert: FakeFleet(), interactive=False, report_dir=tmp_path)
    try:
        assert frame.table is None and frame.bec is None   # nothing from earlier sessions
        assert frame.readers_label.GetLabel() == "No CSV file loaded."
        assert frame.bec_label.GetLabel() == "No configuration loaded."
        assert frame.estimate_label.GetLabel() == "Estimated duration: -"
        assert frame.parallel_text.GetValue() == "all" and frame.rate_spin.GetValue() == 10
        assert frame.retries_spin.GetValue() == 1 and frame.plain_check.GetValue() and not frame.factory_check.GetValue()
        assert frame.command_line() == "" and not frame.copy_button.IsEnabled()
        minimum = frame.GetMinSize()                      # derived from the layout, not a fixed number
        assert minimum.height <= frame.FromDIP(720)        # fits the work area of a 1366x768 screen
        frame.SetSize(minimum)
        frame.Layout()
        frame.panel.Layout()
        assert frame.error_list.GetSize().height >= 50 and frame.log_panel.GetSize().height >= 70
        assert not frame.start_run(confirm=False)
        assert "BEC" in frame.last_error
        assert not frame.load_csv(tmp_path / "missing.csv")
        assert "not found" in frame.last_error
        frame.parallel_text.SetValue("x")
        with pytest.raises(Exception):
            frame.run_options()
    finally:
        frame.timer.Stop()
        frame.Destroy()
        wx.Yield()
