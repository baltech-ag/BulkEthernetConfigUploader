import pytest

from becu.core.csv_import import ReaderTarget
from becu.core.errors import AccessDeniedError, ConfigConflictError, ErrorClass, UploadError
from becu.core.job import JobOptions, ReaderJob, State
from tests.fakes import FakeFleet, make_bec

ADDR = "192.168.1.10:2939"


def options(**kwargs) -> JobOptions:
    base = dict(reboot_wait=0.0, reboot_timeout=1.0, reboot_poll=0.01)
    base.update(kwargs)
    return JobOptions(**base)


def run_job(fleet: FakeFleet, bec, opts: JobOptions | None = None):
    states = []
    job = ReaderJob(ReaderTarget(1, "192.168.1.10", 2939, ADDR), bec, opts or options(), fleet,
                    lambda t, s, m: states.append(s))
    return job.run(), states


def test_normal_update():
    fleet = FakeFleet()
    reader = fleet.add(ADDR)
    bec = make_bec()
    result, states = run_job(fleet, bec)
    assert result.state == State.UPDATED and result.ok
    assert states == [State.HANDSHAKING, State.UPDATING, State.REBOOTING, State.UPDATED]
    assert reader.uploads == 1 and reader.pki_opens == 1
    assert result.info_before.config_id == "99900-0002-0000-01"
    assert result.info_after.config_id == bec.config_id
    assert result.info_after.serial == "12345678" and result.info_after.firmware_version == "2.40.03"
    assert set(result.durations) == {"precheck", "handshake", "upload", "reboot"}
    assert result.duration_s is not None and result.attempts == 1


def test_already_up_to_date_skips_pki():
    fleet = FakeFleet()
    reader = fleet.add(ADDR, config_id="99900-0001-0000-01", config_name="Config A")
    result, states = run_job(fleet, make_bec())
    assert result.state == State.UPDATED and result.note == "already up to date"
    assert states == [State.HANDSHAKING, State.UPDATED]
    assert reader.pki_opens == 0 and reader.uploads == 0


def test_without_plain_access_not_verified():
    fleet = FakeFleet()
    reader = fleet.add(ADDR)
    result, states = run_job(fleet, make_bec(), options(plain_access=False))
    assert result.state == State.UPDATED and result.note == "not verified"
    assert reader.plain_opens == 0 and reader.uploads == 1
    assert result.info_before is None and result.info_after is None


def test_unreachable():
    fleet = FakeFleet()
    fleet.add(ADDR, reachable=False)
    result, _ = run_job(fleet, make_bec())
    assert result.state == State.ERROR and result.error_class == ErrorClass.UNREACHABLE
    assert "connect timeout" in result.error_message


def test_busy_and_pki_failure():
    fleet = FakeFleet()
    fleet.add(ADDR, busy=True)
    assert run_job(fleet, make_bec())[0].error_class == ErrorClass.BUSY
    fleet = FakeFleet()
    fleet.add(ADDR, pki_ok=False)
    result, states = run_job(fleet, make_bec())
    assert result.error_class == ErrorClass.PKI_FAILED and states[-1] == State.ERROR


def test_reboot_timeout_and_verify_mismatch():
    fleet = FakeFleet()
    fleet.add(ADDR, reboot_duration=5.0)
    result, _ = run_job(fleet, make_bec(), options(reboot_timeout=0.2))
    assert result.error_class == ErrorClass.REBOOT_TIMEOUT
    fleet = FakeFleet()
    fleet.add(ADDR, boots_to_config="99900-0003-0000-01")
    result, _ = run_job(fleet, make_bec(), options(reboot_timeout=0.2))
    assert result.error_class == ErrorClass.VERIFY_MISMATCH
    assert "99900-0003-0000-01" in result.error_message


def test_reader_that_reboots_slowly_is_verified():
    fleet = FakeFleet()
    fleet.add(ADDR, reboot_duration=0.3)
    result, _ = run_job(fleet, make_bec(), options(reboot_timeout=3.0))
    assert result.state == State.UPDATED


@pytest.mark.parametrize("error", [AccessDeniedError("denied"), ConfigConflictError("security code")])
def test_upload_refused_without_factory_reset(error):
    fleet = FakeFleet()
    reader = fleet.add(ADDR, upload_error=error)
    result, _ = run_job(fleet, make_bec())
    assert result.state == State.ERROR and result.error_class == error.error_class
    assert reader.factory_resets == 0


def test_factory_reset_on_failure():
    fleet = FakeFleet()
    reader = fleet.add(ADDR, upload_error=ConfigConflictError("security code"))
    result, states = run_job(fleet, make_bec(), options(factory_reset_on_failure=True))
    assert result.state == State.UPDATED and result.note == "after factory reset"
    assert reader.factory_resets == 1 and reader.uploads == 2 and reader.pki_opens == 2
    assert State.REBOOTING in states and states[-1] == State.UPDATED


def test_upload_communication_error_is_retriable():
    fleet = FakeFleet()
    fleet.add(ADDR, upload_error=UploadError("timeout"))
    result, _ = run_job(fleet, make_bec())
    assert result.error_class == ErrorClass.UPLOAD_FAILED and result.error_class in __import__(
        "becu.core.errors", fromlist=["RETRIABLE"]).RETRIABLE


def test_unexpected_exception_is_internal_error():
    fleet = FakeFleet()
    reader = fleet.add(ADDR)
    reader.info = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
    result, _ = run_job(fleet, make_bec())
    assert result.state == State.ERROR and result.error_class == ErrorClass.INTERNAL
    assert "RuntimeError" in result.error_message
