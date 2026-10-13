"""Mock fleet: many simulated readers on loopback addresses 127.0.x.y, one asyncio server each.

Usage::

    uv run python -m mock --count 1800 --bec project.bec --faults unreachable=10,busy=5,locked=5

The fleet writes ``mock/readers.csv`` (input for the tool) and ``mock/expected.csv`` (state and error
class the tool should report per reader) and prints its counters until Ctrl-C.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .reader import EXPECTED, Fault, MockReader, Stats, Timing
from .testpki import ZIP_NAME, TestPki

logger = logging.getLogger("mock.fleet")
MOCK_DIR = Path(__file__).resolve().parent
DEFAULT_INITIAL = ("99900-0001-0000-01", "Config A")
DEFAULT_TARGET = ("99900-0002-0000-01", "Config B")


def address_for(index: int, first_octet3: int = 1) -> str:
    """127.0.<a>.<b> for index 0..n: b runs 1..250 then a increments."""
    return f"127.0.{first_octet3 + index // 250}.{index % 250 + 1}"


@dataclass
class FleetConfig:
    count: int = 1800
    port: int = 2939
    first_octet3: int = 1
    pki_dir: Path = MOCK_DIR / "test_pki"
    target: tuple[str, str] = DEFAULT_TARGET
    initial: tuple[str, str] | None = DEFAULT_INITIAL
    faults: dict[Fault, int] = field(default_factory=dict)
    time_scale: float = 1.0
    csv_path: Path = MOCK_DIR / "readers.csv"
    expected_path: Path = MOCK_DIR / "expected.csv"


class Fleet:
    def __init__(self, config: FleetConfig) -> None:
        self.config = config
        self.pki = TestPki.load_or_generate(config.pki_dir)
        self.zip_path = config.pki_dir / ZIP_NAME
        self.readers: list[MockReader] = []
        self.servers: list[asyncio.base_events.Server] = []
        timing = Timing(scale=config.time_scale)
        faults = self._assign_faults()
        for i in range(config.count):
            self.readers.append(MockReader(i, address_for(i, config.first_octet3), f"{90000000 + i:08d}", self.pki,
                                           config.target, config.initial, faults.get(i, Fault.NONE), timing))
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.started = threading.Event()

    def _assign_faults(self) -> dict[int, Fault]:
        """Faults go to the last readers, in the order given, so the first readers stay plain."""
        total = sum(self.config.faults.values())
        if total > self.config.count:
            raise ValueError(f"{total} faults for {self.config.count} readers")
        assignment: dict[int, Fault] = {}
        index = self.config.count - total
        for fault, n in self.config.faults.items():
            for _ in range(n):
                assignment[index] = fault
                index += 1
        return assignment

    # ---- files ---------------------------------------------------------------------------------------

    def write_csv(self) -> Path:
        with self.config.csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(["Name", "IP", "Port"])
            for r in self.readers:
                writer.writerow([f"mock-{r.index:04d}", r.address, str(self.config.port)])
        return self.config.csv_path

    def write_expected(self) -> Path:
        with self.config.expected_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(["address", "serial", "fault", "expected_state", "expected_error_class"])
            for r in self.readers:
                state, error_class = EXPECTED[r.fault]
                writer.writerow([f"{r.address}:{self.config.port}", r.serial, r.fault.value, state, error_class])
        return self.config.expected_path

    # ---- serving -------------------------------------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        for r in self.readers:
            if r.fault == Fault.UNREACHABLE:
                continue
            self.servers.append(await asyncio.start_server(r.serve, r.address, self.config.port, backlog=8))
        self.started.set()

    async def stop(self) -> None:
        for server in self.servers:
            server.close()
        for server in self.servers:
            await server.wait_closed()
        self.servers = []

    async def serve(self, stop: asyncio.Event) -> None:
        await self.start()
        try:
            await stop.wait()
        finally:
            await self.stop()

    def run_in_thread(self) -> threading.Thread:
        """Start the fleet in a background thread (for tests). Returns the thread; call shutdown() later."""
        def main() -> None:
            async def body() -> None:
                self._stop = asyncio.Event()
                await self.serve(self._stop)
            asyncio.run(body())

        thread = threading.Thread(target=main, name="mock-fleet", daemon=True)
        thread.start()
        if not self.started.wait(60):
            raise RuntimeError("fleet did not start")
        return thread

    def shutdown(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)

    def stats(self) -> Stats:
        total = Stats()
        for r in self.readers:
            for name in vars(total):
                setattr(total, name, getattr(total, name) + getattr(r.stats, name))
        return total

    def busy_count(self) -> int:
        return sum(1 for r in self.readers if r.connection_active)

    def rebooting_count(self) -> int:
        return sum(1 for r in self.readers if r.is_rebooting())


def parse_faults(text: str) -> dict[Fault, int]:
    faults: dict[Fault, int] = {}
    for item in filter(None, (part.strip() for part in text.split(","))):
        name, _, number = item.partition("=")
        faults[Fault(name.strip().lower())] = int(number or 1)
    return faults


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mock", description="Simulated Ethernet card reader fleet for load tests.")
    parser.add_argument("--count", type=int, default=1800)
    parser.add_argument("--port", type=int, default=2939)
    parser.add_argument("--first-octet3", type=int, default=1, help="readers use 127.0.<this>.1 upwards")
    parser.add_argument("--bec", type=Path, help="configuration the tool will upload (sets the target id and name)")
    parser.add_argument("--target-config", default=DEFAULT_TARGET[0])
    parser.add_argument("--target-name", default=DEFAULT_TARGET[1])
    parser.add_argument("--initial-config", default=DEFAULT_INITIAL[0], help="'none' for readers without configuration")
    parser.add_argument("--initial-name", default=DEFAULT_INITIAL[1])
    parser.add_argument("--faults", default="", metavar="NAME=N,...",
                        help="e.g. uptodate=100,unreachable=10,busy=5,locked=5,denied=3,conflict=3,drop=5,mismatch=2,slow=20")
    parser.add_argument("--time-scale", type=float, default=1.0, help="1.0 = real reader timing, 0 = no delays")
    parser.add_argument("--pki-dir", type=Path, default=MOCK_DIR / "test_pki")
    parser.add_argument("--csv", type=Path, default=MOCK_DIR / "readers.csv")
    parser.add_argument("--expected", type=Path, default=MOCK_DIR / "expected.csv")
    parser.add_argument("--stats-interval", type=float, default=5.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    target = (args.target_config, args.target_name)
    if args.bec is not None:
        sys.path.insert(0, str(MOCK_DIR.parent))
        from becu.core.bec_file import load_bec
        bec = load_bec(args.bec)
        target = (bec.config_id, bec.config_name)
    initial = None if args.initial_config.lower() == "none" else (args.initial_config, args.initial_name)
    config = FleetConfig(count=args.count, port=args.port, first_octet3=args.first_octet3, pki_dir=args.pki_dir,
                         target=target, initial=initial, faults=parse_faults(args.faults),
                         time_scale=args.time_scale, csv_path=args.csv, expected_path=args.expected)
    fleet = Fleet(config)
    print(f"readers:     {config.count} on 127.0.{config.first_octet3}.1 .. {fleet.readers[-1].address}, port {config.port}", flush=True)
    print(f"target:      {target[0]} '{target[1]}', initial: {initial}", flush=True)
    print(f"faults:      {', '.join(f'{k.value}={v}' for k, v in config.faults.items()) or 'none'}", flush=True)
    print(f"certificate: {fleet.zip_path}  (use --cert)", flush=True)
    print(f"reader list: {fleet.write_csv()}", flush=True)
    print(f"expected:    {fleet.write_expected()}", flush=True)

    async def run() -> None:
        stop = asyncio.Event()
        await fleet.start()
        print(f"listening, time scale {config.time_scale:g}. Ctrl-C stops the fleet.", flush=True)
        t0 = time.monotonic()
        try:
            while True:
                await asyncio.sleep(args.stats_interval)
                s = fleet.stats()
                print(f"[{time.monotonic() - t0:6.0f} s] connections {s.connections} | open {fleet.busy_count()} | "
                      f"handshakes {s.handshakes} | uploads {s.uploads} | rebooting {fleet.rebooting_count()} | "
                      f"reboots {s.reboots} | busy-rejected {s.rejected_busy} | status errors {s.status_errors}")
        finally:
            stop.set()
            await fleet.stop()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("stopped", flush=True)
    return 0
