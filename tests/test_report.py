import csv
from datetime import datetime

from becu.core.csv_import import ReaderTarget
from becu.core.errors import ErrorClass
from becu.core.job import ReaderResult, State
from becu.core.report import COLUMNS, write_report
from becu.core.session_api import ReaderInfo


def test_write_report(tmp_path):
    t1 = ReaderTarget(1, "10.0.0.1", 2939, "10.0.0.1")
    t2 = ReaderTarget(2, "10.0.0.2", 2940, "10.0.0.2:2940")
    before = ReaderInfo.from_strings("1096 NUANCE RD 2.40.03 04/01/22 12345678", "99900-0002-0000-01", "Config B")
    after = ReaderInfo.from_strings("1096 NUANCE RD 2.40.03 04/01/22 12345678", "99900-0001-0000-01", "Config A")
    ok = ReaderResult(t1, State.UPDATED, info_before=before, info_after=after, attempts=1,
                      started_at=datetime(2026, 10, 13, 12, 0, 0), finished_at=datetime(2026, 10, 13, 12, 0, 35))
    bad = ReaderResult(t2, State.ERROR, error_class=ErrorClass.UNREACHABLE, error_message="connect timeout",
                       attempts=2, started_at=datetime(2026, 10, 13, 12, 0, 0), finished_at=datetime(2026, 10, 13, 12, 0, 2))
    pending = ReaderResult(ReaderTarget(3, "10.0.0.3", 2939, ""), note="not started (stopped)")
    path = write_report(tmp_path / "out" / "r.csv", [ok, bad, pending])
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    assert list(rows[0]) == list(COLUMNS)
    assert rows[0]["address"] == "10.0.0.1:2939" and rows[0]["serial"] == "12345678"
    assert rows[0]["firmware"] == "1096 2.40.03"
    assert rows[0]["config_before"] == "99900-0002-0000-01" and rows[0]["config_after"] == "99900-0001-0000-01"
    assert rows[0]["state"] == "updated" and rows[0]["duration_s"] == "35.0"
    assert rows[1]["state"] == "error" and rows[1]["error_class"] == "unreachable" and rows[1]["attempts"] == "2"
    assert rows[2]["state"] == "pending" and rows[2]["note"] == "not started (stopped)" and rows[2]["duration_s"] == ""
