"""Timecode parse/format round-trips to the tenth."""

from __future__ import annotations

import pytest

from frameweave.util import timecode

ROUND_TRIP = [
    0,
    0.1,
    0.4,
    1,
    1.4,
    9.9,
    59.9,
    60,
    61.5,
    90,
    123,
    123.4,
    3599.9,
    3600,
    3661.4,
    7322.7,
    36000,
]


@pytest.mark.parametrize("seconds", ROUND_TRIP)
def test_parse_format_round_trips_to_the_tenth(seconds: float) -> None:
    rendered = timecode.format(seconds)
    assert rendered.count(":") == 2
    assert "." in rendered
    assert timecode.parse(rendered) == pytest.approx(round(seconds, 1), abs=1e-9)


def test_parse_accepts_clock_and_bare_forms() -> None:
    assert timecode.parse("00:00:00") == 0.0
    assert timecode.parse("1:30") == 90.0
    assert timecode.parse("01:30") == 90.0
    assert timecode.parse("00:01:30") == 90.0
    assert timecode.parse("00:01:30.5") == 90.5
    assert timecode.parse("00:01:30.50") == 90.5
    assert timecode.parse("00:01:30.500") == 90.5
    assert timecode.parse("01:01:01.4") == 3661.4
    assert timecode.parse("123") == 123.0
    assert timecode.parse("123.4") == 123.4


def test_format_without_tenths() -> None:
    assert timecode.format(90, tenths=False) == "00:01:30"
    assert timecode.format(3661.4, tenths=False) == "01:01:01"


def test_format_with_tenths() -> None:
    assert timecode.format(90) == "00:01:30.0"
    assert timecode.format(3661.4) == "01:01:01.4"


def test_negative_raises() -> None:
    with pytest.raises(ValueError, match="negative"):
        timecode.parse("-1")
    with pytest.raises(ValueError, match="negative"):
        timecode.parse("-00:00:01")
    with pytest.raises(ValueError, match="negative"):
        timecode.format(-0.1)
