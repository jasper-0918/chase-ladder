"""Sending hours. Spec sections 4.3 and 12.

`in_send_window` is one of the two functions that carry the engineering story (the
other is `claim()` in db.py). The tests in `tests/test_window.py` were written first
and this body was written against them.

What it does:

    Take an aware UTC datetime and a SendWindow. Convert the instant into the window's
    IANA zone. Return True when the local weekday is in `days` AND the local clock time
    is at or after `start` and strictly before `end`. Otherwise False.

Why the order of operations is the whole design:

    The two DST dates in the tests are the point. On 2026-10-04 Sydney jumps from
    02:00 to 03:00, so an hour does not exist; on 2026-11-01 Chicago repeats 01:00 to
    02:00, so an hour happens twice. Converting the UTC instant first and reading the
    local wall clock afterwards handles both without a special case. Doing the
    arithmetic in the other order, adding a fixed offset to a naive local time, quietly
    breaks twice a year.

    Hence `.astimezone(ZoneInfo(tz))`, never a hand-rolled offset, and the comparison
    reads `local.time()` rather than building datetimes.

The comparison is half open, `start <= t < end`, so a window ending at 18:00 does not
send at 18:00 exactly.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from chase.config import DAYS, SendWindow


def in_send_window(now_utc: datetime, window: SendWindow) -> bool:
    """Is `now_utc` inside this client's sending hours? See the module docstring."""
    local = now_utc.astimezone(ZoneInfo(window.tz))
    if DAYS[local.weekday()] not in window.days:
        return False
    return window.start <= local.time() < window.end


def local_weekday(now_utc: datetime, tz: str) -> str:
    """`mon` through `sun` for an instant in a zone. Provided, because it is plumbing."""
    from zoneinfo import ZoneInfo

    return DAYS[now_utc.astimezone(ZoneInfo(tz)).weekday()]
