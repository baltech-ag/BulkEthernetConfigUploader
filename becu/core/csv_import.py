"""CSV import: read the reader list, detect header and delimiter, turn columns into reader targets.

The address column holds ``a.b.c.d`` or ``a.b.c.d:port``. Port precedence per row:
port in the address > optional port column > default port.
"""

from __future__ import annotations

import csv
import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import CsvFormatError

DEFAULT_PORT = 2939
_ENCODINGS = ("utf-8-sig", "cp1252")
_DELIMITERS = ";,\t|"
_SAMPLE_BYTES = 64 * 1024
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,252}[A-Za-z0-9])?$")


@dataclass
class CsvTable:
    path: Path
    encoding: str
    delimiter: str
    has_header: bool
    header_detected: bool            #: True when has_header came from the heuristic, False when the caller forced it
    columns: list[str]               #: display names, one per column
    rows: list[list[str]]            #: data rows only (header removed), all padded to len(columns)

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass
class AddressStats:
    total: int = 0
    empty: int = 0
    valid: int = 0
    with_port: int = 0
    invalid: int = 0


@dataclass(frozen=True)
class ReaderTarget:
    row_number: int      #: 1-based data row number (header not counted)
    host: str
    port: int
    raw: str             #: cell text the target came from

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class RowProblem:
    row_number: int
    raw: str
    reason: str


@dataclass
class TargetSet:
    targets: list[ReaderTarget] = field(default_factory=list)
    problems: list[RowProblem] = field(default_factory=list)


def parse_address(text: str) -> tuple[str, int | None]:
    """Return ``(host, port)`` for ``host`` or ``host:port``. ``port`` is None when absent.

    Raises ``ValueError`` for empty text, bad ports, IPv6 (not supported by the reader
    transport) and host names with invalid characters.
    """
    value = text.strip()
    if not value:
        raise ValueError("empty")
    if value.count(":") > 1 or value.startswith("["):
        raise ValueError("IPv6 addresses are not supported")
    host, sep, port_text = value.partition(":")
    host = host.strip()
    port: int | None = None
    if sep:
        port = _parse_port(port_text)
    if not host:
        raise ValueError("missing host")
    try:
        ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        # host names are accepted when they look like DNS names (at least one dot, not only digits)
        if not _HOSTNAME_RE.match(host) or "." not in host or host.replace(".", "").isdigit():
            raise ValueError(f"invalid address {host!r}") from None
    return host, port


def _parse_port(text: str) -> int:
    text = text.strip()
    if not text.isdigit():
        raise ValueError(f"invalid port {text!r}")
    port = int(text)
    if not 1 <= port <= 65535:
        raise ValueError(f"port out of range: {port}")
    return port


def _looks_like_address(text: str) -> bool:
    try:
        host, _ = parse_address(text)
    except ValueError:
        return False
    # for the header heuristic only numeric IPv4 counts; host names could be header words
    try:
        ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        return False
    return True


def _read_text(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise CsvFormatError(f"{path.name}: cannot decode as {' or '.join(_ENCODINGS)}")


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=_DELIMITERS).delimiter
    except csv.Error:
        # single column or ambiguous: prefer the first delimiter that occurs at all
        for candidate in _DELIMITERS:
            if candidate in sample:
                return candidate
        return ","


def _detect_header(raw_rows: list[list[str]], sample: str) -> bool:
    """Header heuristic: the first row has no IPv4 address in the column where the other rows have most."""
    if len(raw_rows) < 2:
        return False
    width = max(len(r) for r in raw_rows)
    best_col, best_count = None, 0
    for col in range(width):
        count = sum(1 for r in raw_rows[1:] if col < len(r) and _looks_like_address(r[col]))
        if count > best_count:
            best_col, best_count = col, count
    if best_col is not None and best_count > 0:
        first = raw_rows[0]
        return not (best_col < len(first) and _looks_like_address(first[best_col]))
    try:
        return csv.Sniffer().has_header(sample)
    except csv.Error:
        return False


def load_csv(path: str | Path, has_header: bool | None = None) -> CsvTable:
    """Read a CSV file. ``has_header=None`` detects the header, True/False forces it."""
    path = Path(path)
    if not path.is_file():
        raise CsvFormatError(f"CSV file not found: {path}")
    text, encoding = _read_text(path)
    if not text.strip():
        raise CsvFormatError(f"{path.name}: file is empty")
    sample = text[:_SAMPLE_BYTES]
    delimiter = _sniff_delimiter(sample)
    raw_rows = [row for row in csv.reader(text.splitlines(), delimiter=delimiter)
                if any(cell.strip() for cell in row)]
    if not raw_rows:
        raise CsvFormatError(f"{path.name}: no data rows")
    detected = has_header is None
    if has_header is None:
        has_header = _detect_header(raw_rows, sample)
    width = max(len(r) for r in raw_rows)
    if has_header:
        header_cells, data_rows = raw_rows[0], raw_rows[1:]
    else:
        header_cells, data_rows = [], raw_rows
    columns = _column_names(header_cells, width)
    rows = [[cell.strip() for cell in r] + [""] * (width - len(r)) for r in data_rows]
    return CsvTable(path, encoding, delimiter, has_header, detected, columns, rows)


def _column_names(header_cells: list[str], width: int) -> list[str]:
    names: list[str] = []
    for i in range(width):
        name = header_cells[i].strip() if i < len(header_cells) else ""
        if not name:
            name = f"Column {i}"  # 0-based, the same index the CLI takes
        base, n = name, 2
        while name in names:
            name = f"{base} ({n})"
            n += 1
        names.append(name)
    return names


def analyze_address_column(table: CsvTable, col: int) -> AddressStats:
    stats = AddressStats()
    for row in table.rows:
        cell = row[col]
        stats.total += 1
        if not cell:
            stats.empty += 1
            continue
        try:
            _, port = parse_address(cell)
        except ValueError:
            stats.invalid += 1
            continue
        stats.valid += 1
        if port is not None:
            stats.with_port += 1
    return stats


def suggest_ip_column(table: CsvTable) -> int | None:
    """Index of the column with most valid addresses, or None when no column has one."""
    best, best_valid = None, 0
    for col in range(len(table.columns)):
        valid = analyze_address_column(table, col).valid
        if valid > best_valid:
            best, best_valid = col, valid
    return best


def column_has_ports(table: CsvTable, col: int) -> bool:
    return analyze_address_column(table, col).with_port > 0


def build_targets(table: CsvTable, ip_col: int, port_col: int | None = None,
                  default_port: int = DEFAULT_PORT) -> TargetSet:
    """Turn the table into reader targets. Invalid or duplicate rows become problems and are skipped."""
    if not 1 <= default_port <= 65535:
        raise CsvFormatError(f"default port out of range: {default_port}")
    result = TargetSet()
    seen: dict[tuple[str, int], int] = {}
    for n, row in enumerate(table.rows, start=1):
        raw = row[ip_col]
        if not raw:
            result.problems.append(RowProblem(n, raw, "empty address"))
            continue
        try:
            host, port = parse_address(raw)
        except ValueError as e:
            result.problems.append(RowProblem(n, raw, str(e)))
            continue
        if port is None and port_col is not None and row[port_col]:
            try:
                port = _parse_port(row[port_col])
            except ValueError as e:
                result.problems.append(RowProblem(n, raw, f"port column: {e}"))
                continue
        if port is None:
            port = default_port
        key = (host.lower(), port)
        if key in seen:
            result.problems.append(RowProblem(n, raw, f"duplicate of row {seen[key]}"))
            continue
        seen[key] = n
        result.targets.append(ReaderTarget(n, host, port, raw))
    return result
