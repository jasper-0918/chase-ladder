"""Spec section 4.2."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from chase import clock


@pytest.mark.parametrize(
    "text, expected",
    [
        ("0d", timedelta()),
        ("13h", timedelta(hours=13)),
        ("4d", timedelta(days=4)),
        ("4d13h", timedelta(days=4, hours=13)),
        ("8d13h", timedelta(days=8, hours=13)),
        ("-2h", timedelta(hours=-2)),
        ("5m", timedelta(minutes=5)),
        ("1d2h3m", timedelta(days=1, hours=2, minutes=3)),
        ("  13h  ", timedelta(hours=13)),
    ],
)
def test_parse_offset_forms(text, expected):
    assert clock.parse_offset(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "4", "4x", "d", "13 h", None])
def test_parse_offset_rejects_nonsense(text):
    """A typo must raise, never silently become zero: an offset that quietly reads 0d
    would send the wrong chases during a recording."""
    with pytest.raises(ValueError):
        clock.parse_offset(text)


def test_offset_is_read_per_call(monkeypatch):
    monkeypatch.setenv(clock.ENV_VAR, "0d")
    assert clock.offset() == timedelta()
    monkeypatch.setenv(clock.ENV_VAR, "4d13h")
    assert clock.offset() == timedelta(days=4, hours=13)


def test_now_is_aware_utc_and_shifted(set_offset):
    before = datetime.now(timezone.utc)
    shifted = set_offset("4d")
    assert shifted.tzinfo is timezone.utc
    delta = shifted - before
    assert timedelta(days=4) - timedelta(seconds=5) < delta < timedelta(days=4, seconds=5)


def test_now_local_renders_the_zone(set_offset):
    set_offset("0d")
    sydney = clock.now_local("Australia/Sydney")
    assert str(sydney.tzinfo) == "Australia/Sydney"
    assert sydney.utcoffset() in (timedelta(hours=10), timedelta(hours=11))


def test_now_local_needs_tzdata():
    """Proves the requirements.txt entry is load-bearing rather than decorative."""
    from zoneinfo import ZoneInfo

    assert ZoneInfo("America/Chicago") is not None
