"""Integration: the real SDK session (brp_lib 3.19 with its C PKI layer) against the mock readers."""

import csv

import pytest

pytest.importorskip("cryptography")

from becu.core.csv_import import ReaderTarget  # noqa: E402
from becu.core.errors import BusyError, ErrorClass, PkiError, UnreachableError  # noqa: E402
from becu.core.job import JobOptions, State  # noqa: E402
from becu.core.reader_session import SdkSession, make_sdk_session_factory  # noqa: E402
from becu.core.scheduler import RunOptions, Scheduler  # noqa: E402
from mock.fleet import Fleet, FleetConfig, local_address  # noqa: E402
from mock.fleet import main as fleet_main  # noqa: E402
from mock.reader import EXPECTED, Fault  # noqa: E402
from tests.fakes import make_bec  # noqa: E402

PORT = 29390
FAULTS = {fault: 1 for fault in Fault if fault != Fault.NONE}   # one reader per fault, after two plain readers
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


def run_options(retries: int = 1, factory_reset: bool = False) -> RunOptions:
    return RunOptions(max_parallel=None, start_rate=1000.0, retries=retries, retry_delay=0.1,
                      job=JobOptions(reboot_wait=0.05, reboot_timeout=5.0, reboot_poll=0.05,
                                     factory_reset_on_failure=factory_reset))


def target_for(reader) -> ReaderTarget:
    return ReaderTarget(reader.index + 1, reader.address, reader.port, reader.endpoint)


def test_plain_and_pki_session_against_mock(fleet):
    reader = fleet.readers[0]
    with SdkSession(reader.endpoint, fleet.zip_path) as session:
        session.open_plain()
        info = session.read_info()
    assert info.serial == reader.serial and info.firmware_version == "2.40.03"
    assert info.config_id == "99900-0001-0000-01" and info.config_name == "Config A"

    with SdkSession(reader.endpoint, fleet.zip_path) as session:
        session.open_pki()                      # the C PKI layer handshakes with the Python mock
        assert reader.stats.handshakes == 1
        info = session.read_info()              # tunneled
        assert info.serial == reader.serial
        blocks_done = []
        session.upload(BEC, lambda done, total: blocks_done.append((done, total)))
    assert blocks_done == [(1, 4), (2, 4), (3, 4), (4, 4)]
    assert reader.stats.uploads == 1 and reader.stats.reboots == 1
    with SdkSession(reader.endpoint, fleet.zip_path) as session:
        session.open_plain()
        assert session.read_info().config_id == BEC.config_id


def test_level0_refuses_configuration_commands(fleet):
    with SdkSession(fleet.readers[1].endpoint, fleet.zip_path) as session:
        session.open_plain()
        with pytest.raises(Exception) as excinfo:
            session.upload(BEC)
    assert excinfo.value.error_class == ErrorClass.ACCESS_DENIED


def test_single_session_per_reader(fleet):
    first = SdkSession(fleet.readers[1].endpoint, fleet.zip_path)
    first.open_plain()
    try:
        second = SdkSession(fleet.readers[1].endpoint, fleet.zip_path)
        with pytest.raises(BusyError):
            second.open_plain()
            second.read_info()
    finally:
        first.close()


def test_faults_seen_through_the_sdk(fleet):
    by_fault = {r.fault: r for r in fleet.readers}
    with pytest.raises(UnreachableError):
        s = SdkSession(by_fault[Fault.UNREACHABLE].endpoint, fleet.zip_path)
        s.open_plain()
    with pytest.raises(PkiError):
        with SdkSession(by_fault[Fault.LOCKED].endpoint, fleet.zip_path) as s:
            s.open_pki()


def test_scheduler_run_matches_expected(fleet):
    """Every fault ends in the state and error class that expected.csv promises (retries 1, no factory reset)."""
    targets = [target_for(r) for r in fleet.readers]
    results = Scheduler(targets, BEC, run_options(), make_sdk_session_factory(fleet.zip_path)).run()
    mismatches = []
    for reader, result in zip(fleet.readers, results):
        state, error_class = EXPECTED[reader.fault]
        got = (result.state.value, result.error_class.value if result.error_class else "")
        if got != (state, error_class):
            mismatches.append(f"{reader.fault.value}: expected {state}/{error_class}, got {got[0]}/{got[1]}: "
                              f"{result.error_message}")
    assert not mismatches, "\n".join(mismatches)
    assert not any(r.error_class == ErrorClass.INTERNAL for r in results)
    by_fault = {r.fault: results[r.index] for r in fleet.readers}
    assert by_fault[Fault.DROP].attempts == 2
    assert by_fault[Fault.UPTODATE].note == "already up to date"
    assert by_fault[Fault.DROP_FINISH].attempts == 2 and by_fault[Fault.DROP_FINISH].note == "already up to date"
    assert by_fault[Fault.POWER_CYCLE].attempts == 2
    assert by_fault[Fault.BUSY_TWICE].attempts == 2
    assert by_fault[Fault.NO_CONFIG].info_before.config_id is None
    assert by_fault[Fault.FW_OTHER].info_after.firmware_version == "" and by_fault[Fault.FW_OTHER].info_after.serial == ""
    assert by_fault[Fault.CERT_REJECTED].error_message.startswith(f"{by_fault[Fault.CERT_REJECTED].target.address} rejected the host certificate")
    assert results[0].state == State.UPDATED and results[0].info_after.config_id == BEC.config_id


def test_busy_twice_needs_two_retries(fleet):
    reader = next(r for r in fleet.readers if r.fault == Fault.BUSY_TWICE)
    reader.rejections_left = 2
    reader.config = fleet.config.initial
    [result] = Scheduler([target_for(reader)], BEC, run_options(retries=2), make_sdk_session_factory(fleet.zip_path)).run()
    assert result.state == State.UPDATED and result.attempts == 3, result.error_message


def test_factory_reset_on_failure_path(fleet):
    reader = next(r for r in fleet.readers if r.fault == Fault.CONFLICT)
    reader.upload_refusals_left = 1
    reader.config = fleet.config.initial
    options = run_options(retries=0, factory_reset=True)
    [result] = Scheduler([target_for(reader)], BEC, options, make_sdk_session_factory(fleet.zip_path)).run()
    assert result.state == State.UPDATED and result.note == "after factory reset", result.error_message
    assert reader.stats.factory_resets == 1


def test_factory_reset_refused_stays_an_error(fleet):
    reader = next(r for r in fleet.readers if r.fault == Fault.RESET_REFUSED)
    options = run_options(retries=0, factory_reset=True)
    [result] = Scheduler([target_for(reader)], BEC, options, make_sdk_session_factory(fleet.zip_path)).run()
    assert result.state == State.ERROR and result.error_class == ErrorClass.ACCESS_DENIED
    assert "factory reset" in result.error_message and reader.stats.factory_resets == 0


@pytest.mark.parametrize("mode, config_kwargs, endpoints", [
    ("bind", dict(bind="127.0.0.1", port=29400), [("127.0.0.1", 29400), ("127.0.0.1", 29401), ("127.0.0.1", 29402)]),
    ("addresses", dict(address_block="127.0.210.0/29", port=29410),
     [("127.0.210.1", 29410), ("127.0.210.2", 29410), ("127.0.210.3", 29410)]),
])
def test_remote_address_modes(tmp_path, mode, config_kwargs, endpoints):
    """A fleet for another machine: one address with one port per reader, or one address per reader."""
    config = FleetConfig(count=3, pki_dir=tmp_path, time_scale=0.0, csv_path=tmp_path / "readers.csv",
                         expected_path=tmp_path / "expected.csv", **config_kwargs)
    fleet = Fleet(config)
    assert [(r.address, r.port) for r in fleet.readers] == endpoints
    fleet.run_in_thread()
    try:
        reader = fleet.readers[2]
        with SdkSession(reader.endpoint, fleet.zip_path) as session:
            session.open_plain()
            assert session.read_info().serial == reader.serial
        rows = list(csv.reader(fleet.write_csv().open(encoding="utf-8"), delimiter=";"))
        address_cell = reader.endpoint if mode == "bind" else reader.address   # ports differ: ip:port in one column
        assert rows[0] == ["Name", "IP", "Port"] and rows[3] == ["mock-0002", address_cell, str(reader.port)]
        expected = list(csv.reader(fleet.write_expected().open(encoding="utf-8"), delimiter=";"))
        assert expected[1][0] == fleet.readers[0].endpoint
    finally:
        fleet.shutdown()


def test_bind_auto_and_bad_values(tmp_path, capsys):
    import ipaddress
    ipaddress.ip_address(local_address())                 # an IP literal, never a host name
    files = ["--pki-dir", str(tmp_path), "--csv", str(tmp_path / "r.csv"), "--expected", str(tmp_path / "e.csv")]
    with pytest.raises(SystemExit):                         # a pasted placeholder instead of an address
        fleet_main(["--count", "1", "--bind", "M_IP"] + files)
    assert "not an IP address" in capsys.readouterr().err
    code = fleet_main(["--count", "1", "--time-scale", "0", "--bind", "203.0.113.5"] + files)   # not ours
    assert code == 2 and "cannot listen on 203.0.113.5:2939" in capsys.readouterr().err


def test_address_block_too_small(tmp_path):
    with pytest.raises(ValueError):
        Fleet(FleetConfig(count=10, address_block="127.0.211.0/30", pki_dir=tmp_path))
