"""Spec sections 4.3 and 10.

These tests were written before `in_send_window` existed and are its specification
(spec section 12): the body was implemented against them, red first, then green.

The DST instants were computed against the tzdata database and confirmed on 2026-08-29:
    2026-10-03T16:00Z is 2026-10-04T03:00+11:00 in Sydney  (02:00 never happens)
    2026-11-01T07:30Z is 2026-11-01T01:30-06:00 in Chicago (01:30 happens twice)
"""

from __future__ import annotations

from datetime import datetime, time, timezone

import pytest

from chase.config import SendWindow, load_client
from chase.window import in_send_window, local_weekday


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


SYDNEY_SUNDAY_WINDOW = SendWindow(days=("sun",), start=time(3, 0), end=time(4, 0), tz="Australia/Sydney")
CHICAGO_SUNDAY_WINDOW = SendWindow(days=("sun",), start=time(1, 0), end=time(2, 0), tz="America/Chicago")


@pytest.mark.parametrize(
    "instant, expected, why",
    [
        ("2026-10-03T15:30", False, "01:30 AEST, before the window opens"),
        ("2026-10-03T16:00", True, "the instant 02:00 AEST becomes 03:00 AEDT, the window opens"),
        ("2026-10-03T16:30", True, "03:30 AEDT, inside"),
        ("2026-10-03T17:00", False, "04:00 AEDT, the window has closed"),
    ],
)
def test_send_window_sydney_2026_10_04_spring_forward(instant, expected, why):
    """The missing hour. A naive local-time implementation lets 02:30 through here."""
    assert in_send_window(utc(instant), SYDNEY_SUNDAY_WINDOW) is expected, why


@pytest.mark.parametrize(
    "instant, expected, why",
    [
        ("2026-11-01T05:30", False, "00:30 CDT, before the window"),
        ("2026-11-01T06:30", True, "01:30 CDT, the first pass through the repeated hour"),
        ("2026-11-01T07:30", True, "01:30 CST, the second pass, still inside"),
        ("2026-11-01T08:30", False, "02:30 CST, after the window"),
    ],
)
def test_send_window_chicago_2026_11_01_fall_back(instant, expected, why):
    """The repeated hour. Both passes are inside a 01:00 to 02:00 window."""
    assert in_send_window(utc(instant), CHICAGO_SUNDAY_WINDOW) is expected, why


@pytest.mark.parametrize(
    "instant, expected, why",
    [
        ("2026-10-01T22:00", True, "Thu 08:00 AEST, the first minute of the window"),
        ("2026-10-01T21:59", False, "07:59 AEST, one minute early"),
        ("2026-10-05T21:00", True, "Tue 08:00 AEDT after the change, the shifted boundary"),
        ("2026-10-05T20:59", False, "07:59 AEDT, one minute early after the change"),
        ("2026-10-02T08:00", False, "Fri 18:00 AEST exactly, the window is half open"),
    ],
)
def test_harbourline_window_across_the_change(instant, expected, why, config_dir):
    """The shipped window, whose boundary moves in UTC when Sydney changes."""
    window = load_client("harbourline", config_dir).send_window
    assert in_send_window(utc(instant), window) is expected, why


@pytest.mark.parametrize(
    "instant, expected, why",
    [
        ("2026-10-30T14:00", True, "Fri 09:00 CDT"),
        ("2026-10-30T13:59", False, "08:59 CDT, one minute early"),
        ("2026-11-02T15:00", True, "Mon 09:00 CST after the change"),
        ("2026-11-02T14:59", False, "08:59 CST, one minute early after the change"),
    ],
)
def test_lakeshore_window_across_the_change(instant, expected, why, config_dir):
    window = load_client("lakeshore", config_dir).send_window
    assert in_send_window(utc(instant), window) is expected, why


def test_send_window_weekend(config_dir):
    """Harbourline rests on Saturday, Lakeshore works it. The policy difference that
    the README diff points at."""
    h = load_client("harbourline", config_dir).send_window
    l = load_client("lakeshore", config_dir).send_window
    saturday_syd = utc("2026-09-05T02:00")   # Sat 12:00 in Sydney
    saturday_chi = utc("2026-09-05T15:00")   # Sat 10:00 in Chicago
    assert in_send_window(saturday_syd, h) is False
    assert in_send_window(saturday_chi, l) is True


def test_local_weekday_is_the_local_day_not_the_utc_day():
    """A Manila evening is already the next day in Sydney, which is exactly the trap
    that would have made every recorded run send nothing."""
    assert local_weekday(utc("2026-09-07T22:00"), "Australia/Sydney") == "tue"
    assert local_weekday(utc("2026-09-07T22:00"), "America/Chicago") == "mon"
