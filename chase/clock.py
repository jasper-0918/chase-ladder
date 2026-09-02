"""The only module in the package that reads the wall clock. Spec section 4.2.

Every other module takes the time it needs as an argument or calls in here, so a demo
can move the calendar without anything else knowing. `tests/test_no_datetime_now.py`
greps the package and fails if a second call site appears.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ENV_VAR = "CHASE_CLOCK_OFFSET"

_OFFSET_RE = re.compile(
    r"""^\s*
    (?P<sign>[-+])?
    (?:(?P<days>\d+)d)?
    (?:(?P<hours>\d+)h)?
    (?:(?P<minutes>\d+)m)?
    (?:(?P<seconds>\d+)s)?
    \s*$""",
    re.VERBOSE,
)


def parse_offset(text: str) -> timedelta:
    """Parse an offset such as `0d`, `13h`, `4d13h`, `-2h`, `5m`.

    Raises ValueError on anything else, because a typo in a recording offset that
    silently became zero would send the wrong chases on camera.
    """
    if text is None:
        raise ValueError("offset is None")
    match = _OFFSET_RE.match(text)
    if not match or not any(match.group(g) for g in ("days", "hours", "minutes", "seconds")):
        raise ValueError(
            f"cannot parse {text!r} as an offset; expected forms like 0d, 13h, 4d13h, -2h, 5m"
        )
    parts = {k: int(v) for k, v in match.groupdict().items() if k != "sign" and v}
    delta = timedelta(**parts)
    return -delta if match.group("sign") == "-" else delta


def offset() -> timedelta:
    """Read the offset fresh on every call, so a flag set before a run is honoured."""
    return parse_offset(os.environ.get(ENV_VAR, "0d"))


def now() -> datetime:
    """Timezone-aware UTC, shifted by the configured offset."""
    return datetime.now(timezone.utc) + offset()


def now_local(tz: str) -> datetime:
    """`now()` rendered in an IANA zone. Needs `tzdata` on Windows (requirements.txt)."""
    return now().astimezone(ZoneInfo(tz))
