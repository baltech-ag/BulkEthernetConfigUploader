import pytest

from becu.core.estimate import describe, estimate_seconds, format_duration, per_reader_seconds


def test_per_reader_grows_with_the_configuration_size():
    assert per_reader_seconds(0) == 30
    assert per_reader_seconds(1000) == 46


def test_estimate_without_limit_is_ramp_plus_one_reader():
    assert estimate_seconds(1800, 50, None, 1000) == pytest.approx(1799 / 50 + 46)
    assert estimate_seconds(1, 50, None, 1000) == 46
    assert estimate_seconds(0, 50, None, 1000) == 0


def test_estimate_with_limit_runs_in_batches():
    assert estimate_seconds(1800, 50, 100, 1000) == pytest.approx(99 / 50 + 18 * 46)
    assert estimate_seconds(10, 50, 100, 1000) == estimate_seconds(10, 50, None, 1000)   # limit above the count


@pytest.mark.parametrize("seconds, text", [
    (3, "about 5 s"), (46, "about 50 s"), (82, "about 2 min"), (3600, "about 1 h"), (3700, "about 1 h 2 min"),
])
def test_format_duration_rounds_up(seconds, text):
    assert format_duration(seconds) == text


def test_describe_names_the_assumptions():
    text = describe(1800, 50, None, 1000)
    assert text.startswith("about 2 min for 1800 readers started at 50 per second, about 46 s per reader")
    assert "retries and errors not included" in text
    assert "at most 100 at a time" in describe(1800, 50, 100, 1000)
