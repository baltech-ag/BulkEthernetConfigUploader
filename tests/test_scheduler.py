import time

import pytest

from becu.core.csv_import import ReaderTarget
from becu.core.errors import ErrorClass
from becu.core.job import JobOptions, State
from becu.core.scheduler import RunOptions, Scheduler
from tests.fakes import FakeFleet, make_bec


def targets(n: int) -> list[ReaderTarget]:
    return [ReaderTarget(i + 1, f"10.0.{i // 250}.{i % 250 + 1}", 2939, "") for i in range(n)]


def fast_options(**kwargs) -> RunOptions:
    job = JobOptions(reboot_wait=0.0, reboot_timeout=0.5, reboot_poll=0.01)
    base = dict(max_parallel=None, start_rate=1000.0, retries=1, retry_delay=0.05, job=job)
    base.update(kwargs)
    return RunOptions(**base)


def test_all_readers_updated_and_counters():
    fleet = FakeFleet()
    tg = targets(40)
    for t in tg:
        fleet.add(t.address, handshake_duration=0.02)
    events = []
    scheduler = Scheduler(tg, make_bec(), fast_options(), fleet, events.append)
    results = scheduler.run()
    assert [r.state for r in results] == [State.UPDATED] * 40
    c = scheduler.counters()
    assert (c.total, c.updated, c.errors, c.pending) == (40, 40, 0, 0)
    assert c.finished == 40 and c.elapsed_s > 0
    assert {e.state for e in events} == {State.HANDSHAKING, State.UPDATING, State.REBOOTING, State.UPDATED}
    assert all(r.attempts == 1 for r in results)


def test_max_parallel_limits_concurrency():
    fleet = FakeFleet()
    tg = targets(12)
    for t in tg:
        fleet.add(t.address, handshake_duration=0.05)
    scheduler = Scheduler(tg, make_bec(), fast_options(max_parallel=3), fleet)
    scheduler.start()
    peak = 0
    while not scheduler.finished.wait(0.005):
        c = scheduler.counters()
        peak = max(peak, c.handshaking + c.updating + c.rebooting)
    assert 1 <= peak <= 3
    assert scheduler.counters().updated == 12


def test_start_rate_ramps():
    fleet = FakeFleet()
    tg = targets(6)
    for t in tg:
        fleet.add(t.address)
    t0 = time.monotonic()
    Scheduler(tg, make_bec(), fast_options(start_rate=20.0), fleet).run()
    assert time.monotonic() - t0 >= 5 / 20.0


def test_retry_after_unreachable_then_success():
    fleet = FakeFleet()
    tg = targets(1)
    reader = fleet.add(tg[0].address, reachable=False)
    events = []

    def sink(event):
        events.append(event)
        if event.state == State.PENDING:  # retry scheduled -> reader comes back
            reader.reachable = True

    scheduler = Scheduler(tg, make_bec(), fast_options(retries=2, retry_delay=0.02), fleet, sink)
    results = scheduler.run()
    assert results[0].state == State.UPDATED and results[0].attempts == 2
    assert any(e.state == State.PENDING and e.message.startswith("retry") for e in events)


def test_no_retry_for_pki_failure_and_retry_limit():
    fleet = FakeFleet()
    tg = targets(2)
    fleet.add(tg[0].address, pki_ok=False)
    fleet.add(tg[1].address, reachable=False)
    results = Scheduler(tg, make_bec(), fast_options(retries=2, retry_delay=0.01), fleet).run()
    assert results[0].error_class == ErrorClass.PKI_FAILED and results[0].attempts == 1
    assert results[1].error_class == ErrorClass.UNREACHABLE and results[1].attempts == 3
    assert Scheduler(tg, make_bec(), fast_options(), fleet).counters().pending == 2


def test_stop_leaves_pending_readers():
    fleet = FakeFleet()
    tg = targets(20)
    for t in tg:
        fleet.add(t.address, handshake_duration=0.2)
    scheduler = Scheduler(tg, make_bec(), fast_options(max_parallel=2, start_rate=1000.0), fleet)
    scheduler.start()
    time.sleep(0.05)
    scheduler.stop()
    assert scheduler.join(5.0)
    results = scheduler.results()
    states = [r.state for r in results]
    assert states.count(State.UPDATED) >= 1
    assert states.count(State.PENDING) >= 1
    assert all(r.note == "not started (stopped)" for r in results if r.state == State.PENDING)


def test_invalid_options_and_empty_run():
    with pytest.raises(ValueError):
        Scheduler([], make_bec(), fast_options(max_parallel=0), FakeFleet())
    with pytest.raises(ValueError):
        Scheduler([], make_bec(), fast_options(start_rate=0), FakeFleet())
    assert Scheduler([], make_bec(), fast_options(), FakeFleet()).run() == []
