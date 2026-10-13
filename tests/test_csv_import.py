from pathlib import Path

import pytest

from becu.core.csv_import import (
    analyze_address_column, build_targets, column_has_ports, load_csv, parse_address,
    suggest_ip_column,
)
from becu.core.errors import CsvFormatError


def write(tmp_path: Path, text: str, name: str = "readers.csv", encoding: str = "utf-8") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding=encoding)
    return path


def test_parse_address_variants():
    assert parse_address("192.168.1.10") == ("192.168.1.10", None)
    assert parse_address(" 192.168.1.10:2939 ") == ("192.168.1.10", 2939)
    assert parse_address("reader-01.example.com:12345") == ("reader-01.example.com", 12345)
    for bad in ("", "192.168.1.10:0", "192.168.1.10:70000", "192.168.1.10:abc", "::1", "[fe80::1]:2939",
                "300.1.1.1", "a b", "nonsense", "Lobby", "1.2.3"):
        with pytest.raises(ValueError):
            parse_address(bad)


def test_header_detected_and_columns_named(tmp_path):
    path = write(tmp_path, "Name;IP;Port\nLobby;192.168.1.10;2939\nOffice;192.168.1.11;\n")
    table = load_csv(path)
    assert table.has_header and table.header_detected
    assert table.delimiter == ";"
    assert table.columns == ["Name", "IP", "Port"]
    assert table.rows == [["Lobby", "192.168.1.10", "2939"], ["Office", "192.168.1.11", ""]]
    assert suggest_ip_column(table) == 1


def test_no_header_single_column(tmp_path):
    path = write(tmp_path, "192.168.1.10\n192.168.1.11:4000\n\n192.168.1.12\n")
    table = load_csv(path)
    assert not table.has_header
    assert table.columns == ["Column 0"]
    assert table.row_count == 3
    assert column_has_ports(table, 0)
    stats = analyze_address_column(table, 0)
    assert (stats.valid, stats.with_port, stats.invalid, stats.empty) == (3, 1, 0, 0)


@pytest.mark.parametrize("text, rows", [
    ("IP\n192.168.1.10\n", 1),                                  # single column, header, one data row
    ("ip address;port\n192.168.1.10;2939\n", 1),                # header words with spaces
    ("Reader IP\r\n10.0.0.1\r\n10.0.0.2\r\n", 2),               # CRLF
    ("192.168.1.10\n", 1),                                      # one data row, no header
])
def test_header_skipped_never_parsed_as_address(tmp_path, text, rows):
    table = load_csv(write(tmp_path, text))
    assert table.row_count == rows
    col = suggest_ip_column(table)
    result = build_targets(table, col)
    assert len(result.targets) == rows and result.problems == []


def test_header_override(tmp_path):
    path = write(tmp_path, "192.168.1.10\n192.168.1.11\n")
    assert load_csv(path, has_header=True).row_count == 1
    assert load_csv(path, has_header=False).row_count == 2


def test_utf8_bom_and_comma(tmp_path):
    path = write(tmp_path, "ip,site\n10.0.0.1,Köln\n", encoding="utf-8-sig")
    table = load_csv(path)
    assert table.encoding == "utf-8-sig" and table.delimiter == ","
    assert table.rows[0][1] == "Köln"


def test_cp1252_fallback(tmp_path):
    path = tmp_path / "r.csv"
    path.write_bytes("ip;site\r\n10.0.0.1;M\xfcnchen\r\n".encode("cp1252"))
    table = load_csv(path)
    assert table.encoding == "cp1252" and table.rows[0][1] == "München"


def test_build_targets_port_precedence_and_problems(tmp_path):
    path = write(tmp_path, "IP;Port\n192.168.1.10:5000;6000\n192.168.1.11;6000\n192.168.1.12;\n"
                           "192.168.1.11;6000\nbad address;1\n;2\n192.168.1.13;x\n")
    table = load_csv(path)
    result = build_targets(table, 0, 1, default_port=2939)
    assert [t.address for t in result.targets] == ["192.168.1.10:5000", "192.168.1.11:6000", "192.168.1.12:2939"]
    assert result.targets[0].row_number == 1
    reasons = [(p.row_number, p.reason) for p in result.problems]
    assert reasons == [(4, "duplicate of row 2"), (5, "invalid address 'bad address'"), (6, "empty address"),
                       (7, "port column: invalid port 'x'")]


def test_build_targets_without_port_column(tmp_path):
    path = write(tmp_path, "192.168.1.10\n192.168.1.11:2940\n")
    table = load_csv(path)
    result = build_targets(table, 0, None, default_port=2939)
    assert [t.address for t in result.targets] == ["192.168.1.10:2939", "192.168.1.11:2940"]
    assert result.problems == []


def test_missing_or_empty_file(tmp_path):
    with pytest.raises(CsvFormatError):
        load_csv(tmp_path / "none.csv")
    with pytest.raises(CsvFormatError):
        load_csv(write(tmp_path, "\n\n"))
