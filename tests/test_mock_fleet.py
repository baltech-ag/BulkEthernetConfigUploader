"""Integration: the real SDK session (brp_lib 3.19 with its C PKI layer) against the mock readers."""

import pytest

pytest.importorskip("cryptography")

from becu.core.csv_import import ReaderTarget  # noqa: E402
from becu.core.errors import BusyError, ErrorClass, PkiError, UnreachableError  # noqa: E402
from becu.core.job import JobOptions, State  # noqa: E402
from becu.core.reader_session import SdkSession, make_sdk_session_factory  # noqa: E402
from becu.core.scheduler import RunOptions, Scheduler  # noqa: E402
from mock.fleet import Fleet, FleetConfig  # noqa: E402
from mock.reader import EXPECTED, Fault  # noqa: E402
from tests.fakes import make_bec  # noqa: E402

PORT = 29390
FAULTS = {Fault.UPTODATE: 1, Fault.UNREACHABLE: 1, Fault.BUSY: 1, Fault.LOCKED: 1, Fault.DENIED: 1,
          Fault.CONFLICT: 1, Fault.DROP: 1, Fault.MISMATCH: 1, Fault.SLOW: 1}
BEC = make_bec(config_id="99900-0002-0000-01", name="Config B", blocks=4)


@pytest.fixture(scope="module")
def fleet(tmp_path_factory):
    pki_dir = tmp_path_factory.mktemp("pki")
    config = FleetConfig(count=2 + sum(FAULTS.values()), port=PORT, first_octet3=200, pki_dir=pki_dir,
                         target=(BEC.config_id, BEC.config_name), faults=FAULTS, time_scale=0.0,
                         csv_path=pki_dir / "readers.csv", expected_path=pki_dir / "expected.csv")
    fleet = Fleet(config)
    fleet.run_in_thread()
    yield fleet
    fleet.shutdown()


def address(fleet: Fleet, index: int) -> str:
    return f"{fleet.readers[index].address}:{PORT}"


def test_plain_and_pki_session_against_mock(fleet):
    reader = fleet.readers[0]
    with SdkSession(address(fleet, 0), fleet.zip_path) as session:
        session.open_plain()
        info = session.read_info()
    assert info.serial == reader.serial and info.firmware_version == "2.40.03"
    assert info.config_id == "99900-0001-0000-01" and info.config_name == "Config A"

    with SdkSession(address(fleet, 0), fleet.zip_path) as session:
        session.open_pki()                      # the C PKI layer handshakes with the Python mock
        assert reader.stats.handshakes == 1
        info = session.read_info()              # tunneled
        assert info.serial == reader.serial
        blocks_done = []
        session.upload(BEC, lambda done, total: blocks_done.append((done, total)))
    assert blocks_done == [(1, 4), (2, 4), (3, 4), (4, 4)]
    assert reader.stats.uploads == 1 and reader.stats.reboots == 1
    with SdkSession(address(fleet, 0), fleet.zip_path) as session:
        session.open_plain()
        assert session.read_info().config_id == BEC.config_id


def test_level0_refuses_configuration_commands(fleet):
    with SdkSession(address(fleet, 1), fleet.zip_path) as session:
        session.open_plain()
        with pytest.raises(Exception) as excinfo:
            session.upload(BEC)
    assert excinfo.value.error_class == ErrorClass.ACCESS_DENIED


def test_single_session_per_reader(fleet):
    first = SdkSession(address(fleet, 1), fleet.zip_path)
    first.open_plain()
    try:
        second = SdkSession(address(fleet, 1), fleet.zip_path)
        with pytest.raises(BusyError):
            second.open_plain()
            second.read_info()
    finally:
        first.close()


def test_faults_seen_through_the_sdk(fleet):
    by_fault = {r.fault: r for r in fleet.readers}
    with pytest.raises(UnreachableError):
        s = SdkSession(f"{by_fault[Fault.UNREACHABLE].address}:{PORT}", fleet.zip_path)
        s.open_plain()
    with pytest.raises(PkiError):
        with SdkSession(f"{by_fault[Fault.LOCKED].address}:{PORT}", fleet.zip_path) as s:
            s.open_pki()


def test_scheduler_run_matches_expected(fleet):
    targets = [ReaderTarget(i + 1, r.address, PORT, r.address) for i, r in enumerate(fleet.readers)]
    options = RunOptions(max_parallel=None, start_rate=1000.0, retries=1, retry_delay=0.1,
                         job=JobOptions(reboot_wait=0.05, reboot_timeout=5.0, reboot_poll=0.05))
    results = Scheduler(targets, BEC, options, make_sdk_session_factory(fleet.zip_path)).run()
    for reader, result in zip(fleet.readers, results):
        state, error_class = EXPECTED[reader.fault]
        assert result.state.value == state, (reader.fault, result.error_message)
        assert (result.error_class.value if result.error_class else "") == error_class, (reader.fault, result.error_message)
    by_fault = {r.fault: r for r in fleet.readers}
    assert results[by_fault[Fault.DROP].index].attempts == 2
    assert results[by_fault[Fault.UPTODATE].index].note == "already up to date"
    assert results[0].state == State.UPDATED and results[0].info_after.config_id == BEC.config_id


def test_factory_reset_on_failure_path(fleet):
    by_fault = {r.fault: r for r in fleet.readers}
    reader = by_fault[Fault.CONFLICT]
    reader.upload_refusals_left = 1
    reader.config = fleet.config.initial
    target = ReaderTarget(1, reader.address, PORT, reader.address)
    options = RunOptions(max_parallel=None, start_rate=1000.0, retries=0, retry_delay=0.1,
                         job=JobOptions(reboot_wait=0.05, reboot_timeout=5.0, reboot_poll=0.05,
                                        factory_reset_on_failure=True))
    [result] = Scheduler([target], BEC, options, make_sdk_session_factory(fleet.zip_path)).run()
    assert result.state == State.UPDATED and result.note == "after factory reset", result.error_message
    assert reader.stats.factory_resets == 1
