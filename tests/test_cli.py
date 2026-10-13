import pytest

from becu.cli import main
from tests.test_bec_file import SAMPLE
from tests.test_pki_package import make_zip


@pytest.fixture
def inputs(tmp_path):
    csv_path = tmp_path / "readers.csv"
    csv_path.write_text("Site;IP\nA;192.168.1.10\nB;192.168.1.11:2940\nC;nonsense\n", encoding="utf-8")
    bec_path = tmp_path / "c.bec"
    bec_path.write_text(SAMPLE, encoding="utf-8")
    zip_path = make_zip(tmp_path / "certs.zip")
    return csv_path, bec_path, zip_path


def test_list_columns(inputs, capsys):
    csv_path, _, _ = inputs
    assert main(["--csv", str(csv_path), "--list-columns"]) == 0
    out = capsys.readouterr().out
    assert "header: yes (detected)" in out
    assert "   1  'IP': 2 addresses (1 with port), 1 invalid" in out   # 0-based column index


def test_dry_run(inputs, capsys):
    csv_path, bec_path, zip_path = inputs
    code = main(["--csv", str(csv_path), "--config", str(bec_path), "--cert", str(zip_path), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "99900-0002-0000-01 'Config B' (3 blocks, 10 bytes)" in out
    assert "2 from readers.csv, 1 rows skipped" in out
    assert "row 3: invalid address 'nonsense'" in out
    assert "192.168.1.10:2939" in out and "192.168.1.11:2940" in out
    assert "estimated:     about 35 s for 2 readers started at 10 per second, about 30 s per reader" in out
    assert "dry run" in out


def test_dry_run_with_addresses(inputs, capsys):
    _, bec_path, zip_path = inputs
    code = main(["--address", "10.1.1.1", "--address", "10.1.1.2:4000", "--config", str(bec_path),
                 "--cert", str(zip_path), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0 and "10.1.1.1:2939" in out and "10.1.1.2:4000" in out


@pytest.mark.parametrize("argv, message", [
    ([], "--csv FILE or at least one --address"),
    (["--address", "10.1.1.1"], "--config FILE is required"),
    (["--list-columns"], "--list-columns needs --csv"),
])
def test_usage_errors(argv, message, capsys):
    assert main(argv) == 2
    assert message in capsys.readouterr().err


def test_bad_inputs_give_exit_2(inputs, capsys, tmp_path):
    csv_path, bec_path, zip_path = inputs
    assert main(["--csv", str(csv_path), "--config", str(tmp_path / "none.bec"), "--cert", str(zip_path), "--dry-run"]) == 2
    assert main(["--csv", str(csv_path), "--config", str(bec_path), "--cert", str(zip_path), "--dry-run",
                 "--ip-column", "5"]) == 2
    assert main(["--csv", str(csv_path), "--config", str(bec_path), "--cert", str(zip_path), "--dry-run",
                 "--max-parallel", "0"]) == 2
    err = capsys.readouterr().err
    assert "--ip-column 5: readers.csv has the columns 0..1" in err and "--max-parallel" in err


def test_column_index_is_zero_based(inputs, capsys):
    csv_path, bec_path, zip_path = inputs
    base = ["--csv", str(csv_path), "--config", str(bec_path), "--cert", str(zip_path), "--dry-run"]
    assert main(base + ["--ip-column", "1"]) == 0
    assert "2 from readers.csv" in capsys.readouterr().out
    assert main(base + ["--ip-column", "0"]) == 2          # column 0 (Site) holds no addresses
    assert "no valid reader addresses" in capsys.readouterr().err
    with pytest.raises(SystemExit):                         # the old --yes flag is gone
        main(base + ["--yes"])
