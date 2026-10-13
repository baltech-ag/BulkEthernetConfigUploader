"""Command line: validate the inputs, run the scheduler, write the report.

Exit codes: 0 all readers updated, 1 at least one error (or stopped), 2 usage or input error.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from becu import __version__
from becu.core.bec_file import BecFile, load_bec
from becu.core.csv_import import (
    DEFAULT_PORT, CsvTable, ReaderTarget, TargetSet, analyze_address_column, build_targets,
    column_has_ports, load_csv, parse_address, suggest_ip_column,
)
from becu.core.errors import InputError
from becu.core.estimate import describe as describe_estimate
from becu.core.job import JobOptions, State
from becu.core.logsetup import setup_logging
from becu.core.pki_package import default_certs_dir, find_default_package, load_pki_package, project_root
from becu.core.report import write_report
from becu.core.scheduler import Counters, JobEvent, RunOptions, Scheduler

EXIT_OK, EXIT_ERRORS, EXIT_USAGE = 0, 1, 2
logger = logging.getLogger("becu.cli")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="becu",
        description="Load one .bec configuration onto many BALTECH Ethernet readers in parallel.")
    p.add_argument("--version", action="version", version=f"becu {__version__}")
    src = p.add_argument_group("readers")
    src.add_argument("--csv", type=Path, help="CSV file with the reader addresses")
    src.add_argument("--address", action="append", default=[], metavar="HOST[:PORT]",
                     help="single reader address, may be repeated (alternative to --csv)")
    src.add_argument("--ip-column", type=int, metavar="N",
                     help="0-based index of the column with the addresses (default: the column with most addresses)")
    src.add_argument("--port-column", type=int, metavar="N", help="0-based index of an optional column with the port")
    hdr = src.add_mutually_exclusive_group()
    hdr.add_argument("--header", dest="header", action="store_true", default=None, help="first CSV row is a header")
    hdr.add_argument("--no-header", dest="header", action="store_false", help="first CSV row is data")
    src.add_argument("--default-port", type=int, default=DEFAULT_PORT, help=f"port when none is given (default {DEFAULT_PORT})")
    src.add_argument("--list-columns", action="store_true", help="show the CSV columns with address statistics and exit")

    cfg = p.add_argument_group("configuration and certificate")
    cfg.add_argument("--config", type=Path, help="the .bec configuration file")
    cfg.add_argument("--cert", type=Path, help=f"certificate package (*.zip); default: the single zip in {default_certs_dir()}")

    run = p.add_argument_group("run")
    run.add_argument("--max-parallel", default="all", metavar="N|all", help="maximum readers in progress at once (default all)")
    run.add_argument("--start-rate", type=float, default=10.0, metavar="N", help="reader starts per second (default 10)")
    run.add_argument("--retries", type=int, default=1, help="additional attempts for unreachable/busy/timeout errors (default 1)")
    run.add_argument("--retry-delay", type=float, default=5.0, metavar="S", help="seconds between attempts (default 5)")
    run.add_argument("--no-plain-access", action="store_true",
                     help="do not read reader info without certificate: no skip of up-to-date readers, no verification")
    run.add_argument("--factory-reset-on-failure", action="store_true",
                     help="on access denied or configuration conflict: factory reset, then upload again")
    run.add_argument("--reboot-timeout", type=float, default=60.0, metavar="S", help="seconds to wait for a reader after the reboot (default 60)")
    run.add_argument("--dry-run", action="store_true", help="validate everything and list the readers, do not connect")
    run.add_argument("--auto-confirm", action="store_true", help="start without the confirmation prompt")

    out = p.add_argument_group("output")
    out.add_argument("--report", type=Path, help="result CSV (default reports/becu-<timestamp>.csv)")
    out.add_argument("--log-dir", type=Path, default=project_root() / "logs", help="log folder (default logs/)")
    out.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="log file level (default INFO)")
    out.add_argument("-v", "--verbose", action="store_true", help="also print the log to the console")
    return p


# ---- input handling --------------------------------------------------------------------------------

def _column_arg(table: CsvTable, index: int | None, what: str) -> int | None:
    if index is None:
        return None
    if not 0 <= index < len(table.columns):
        raise InputError(f"{what} {index}: {table.path.name} has the columns 0..{len(table.columns) - 1} "
                         "(see --list-columns)")
    return index


def describe_columns(table: CsvTable) -> str:
    lines = [f"{table.path.name}: {table.row_count} data rows, delimiter {table.delimiter!r}, "
             f"encoding {table.encoding}, header: {'yes' if table.has_header else 'no'}"
             f"{' (detected)' if table.header_detected else ''}"]
    for i, name in enumerate(table.columns):
        s = analyze_address_column(table, i)
        lines.append(f"  {i:2d}  {name!r}: {s.valid} addresses ({s.with_port} with port), "
                     f"{s.invalid} invalid, {s.empty} empty")
    return "\n".join(lines)


def targets_from_args(args: argparse.Namespace) -> tuple[TargetSet, CsvTable | None]:
    if args.csv is None and not args.address:
        raise InputError("give --csv FILE or at least one --address")
    if args.csv is not None and args.address:
        raise InputError("use either --csv or --address, not both")
    if args.address:
        targets: list[ReaderTarget] = []
        for n, text in enumerate(args.address, start=1):
            try:
                host, port = parse_address(text)
            except ValueError as e:
                raise InputError(f"--address {text!r}: {e}") from None
            targets.append(ReaderTarget(n, host, port or args.default_port, text))
        return TargetSet(targets=targets), None
    table = load_csv(args.csv, has_header=args.header)
    ip_col = _column_arg(table, args.ip_column, "--ip-column")
    if ip_col is None:
        ip_col = suggest_ip_column(table)
        if ip_col is None:
            raise InputError(f"no column with IP addresses found in {table.path.name}; use --list-columns")
    port_col = _column_arg(table, args.port_column, "--port-column")
    if port_col is not None and column_has_ports(table, ip_col):
        logger.info("address column already contains ports; the port column is used only for rows without one")
    return build_targets(table, ip_col, port_col, args.default_port), table


def parse_max_parallel(value: str) -> int | None:
    if value.strip().lower() == "all":
        return None
    if not value.strip().isdigit() or int(value) < 1:
        raise InputError("--max-parallel must be a positive number or 'all'")
    return int(value)


# ---- console progress ------------------------------------------------------------------------------

def format_counters(c: Counters) -> str:
    return (f"pending {c.pending:5d} | handshaking {c.handshaking:5d} | updating {c.updating:5d} | "
            f"rebooting {c.rebooting:5d} | updated {c.updated:5d} | errors {c.errors:5d} | "
            f"total {c.total} | {c.elapsed_s:6.0f} s")


class ConsoleProgress:
    def __init__(self, scheduler: Scheduler, stream=None) -> None:
        self.scheduler = scheduler
        self.stream = stream or sys.stdout
        self.tty = hasattr(self.stream, "isatty") and self.stream.isatty()
        self._last_line = ""

    def on_event(self, event: JobEvent) -> None:
        if event.state == State.ERROR:
            self._print_line(f"  ERROR {event.target.address}: {event.message}")
        elif event.state == State.PENDING and event.message.startswith("retry"):
            self._print_line(f"  RETRY {event.target.address}: {event.message}")

    def _print_line(self, text: str) -> None:
        if self.tty and self._last_line:
            self.stream.write("\r" + " " * len(self._last_line) + "\r")
        self.stream.write(text + "\n")
        self.stream.flush()
        self._last_line = ""

    def update(self, final: bool = False) -> None:
        line = format_counters(self.scheduler.counters())
        if self.tty and not final:
            self.stream.write("\r" + line)
            self._last_line = line
        else:
            self.stream.write(("\r" if self.tty else "") + line + "\n")
            self._last_line = ""
        self.stream.flush()


# ---- main ------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _main(args)
    except InputError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE


def _main(args: argparse.Namespace) -> int:
    if args.list_columns:
        if args.csv is None:
            raise InputError("--list-columns needs --csv")
        print(describe_columns(load_csv(args.csv, has_header=args.header)))
        return EXIT_OK

    if args.csv is None and not args.address:
        raise InputError("give --csv FILE or at least one --address")
    if args.config is None:
        raise InputError("--config FILE is required")
    bec: BecFile = load_bec(args.config)
    cert_path = args.cert or find_default_package()
    package = load_pki_package(cert_path)
    target_set, table = targets_from_args(args)
    max_parallel = parse_max_parallel(args.max_parallel)
    if args.start_rate <= 0 or args.retries < 0 or args.retry_delay < 0 or args.reboot_timeout <= 0:
        raise InputError("--start-rate and --reboot-timeout must be > 0, --retries and --retry-delay >= 0")

    print(f"configuration: {bec.config_id} {bec.config_name!r} ({bec.block_count} blocks, {bec.payload_size} bytes)")
    print(f"certificate:   {package.describe()}")
    if table is not None:
        print(f"readers:       {len(target_set.targets)} from {table.path.name}"
              + (f", {len(target_set.problems)} rows skipped" if target_set.problems else ""))
    else:
        print(f"readers:       {len(target_set.targets)} from the command line")
    for problem in target_set.problems[:20]:
        print(f"  row {problem.row_number}: {problem.reason} ({problem.raw!r})")
    if len(target_set.problems) > 20:
        print(f"  ... {len(target_set.problems) - 20} more")
    if not target_set.targets:
        raise InputError("no valid reader addresses")
    if bec.is_draft:
        print("warning: the configuration is a DRAFT")
    print(f"estimated:     {describe_estimate(len(target_set.targets), args.start_rate, max_parallel, bec.payload_size)}")

    if args.dry_run:
        for t in target_set.targets:
            print(f"  {t.address}")
        print("dry run: nothing was sent")
        return EXIT_OK

    if not args.auto_confirm:
        if not sys.stdin.isatty():
            raise InputError("no terminal for the confirmation prompt; pass --auto-confirm")
        answer = input(f"Update {len(target_set.targets)} readers with {bec.config_id} {bec.config_name!r}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return EXIT_USAGE

    log_file = setup_logging(args.log_dir, args.log_level, console_level="INFO" if args.verbose else None)
    logger.info("becu %s: %d readers, config %s, certificate %s", __version__, len(target_set.targets),
                bec.config_id, package.path.name)
    report_path = args.report or (project_root() / "reports" / f"becu-{datetime.now():%Y%m%d-%H%M%S}.csv")

    from becu.core.reader_session import make_sdk_session_factory
    options = RunOptions(
        max_parallel=max_parallel, start_rate=args.start_rate, retries=args.retries,
        retry_delay=args.retry_delay,
        job=JobOptions(plain_access=not args.no_plain_access,
                       factory_reset_on_failure=args.factory_reset_on_failure,
                       reboot_timeout=args.reboot_timeout))
    scheduler = Scheduler(target_set.targets, bec, options, make_sdk_session_factory(package.path))
    progress = ConsoleProgress(scheduler)
    scheduler.on_event = progress.on_event

    print(f"log file:      {log_file}")
    print(f"max parallel {max_parallel or 'all'}, start rate {args.start_rate:g}/s, retries {args.retries}")
    scheduler.start()
    stopped = False
    try:
        while not scheduler.finished.wait(1.0):
            progress.update()
    except KeyboardInterrupt:
        stopped = True
        scheduler.stop()
        print("\nstopping: no further readers are started, running readers finish ...")
        while not scheduler.finished.wait(1.0):
            progress.update()
    progress.update(final=True)

    results = scheduler.results()
    write_report(report_path, results)
    counters = scheduler.counters()
    print(f"report:        {report_path}")
    print(f"result:        {counters.updated} updated, {counters.errors} errors, {counters.pending} not started "
          f"in {counters.elapsed_s:.0f} s")
    logger.info("finished: %s", counters.as_dict())
    if stopped or counters.errors or counters.pending:
        return EXIT_ERRORS
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
