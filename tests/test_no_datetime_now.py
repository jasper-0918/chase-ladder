"""Spec sections 4.2 and 10.

One rule with one test behind it: `clock.py` is the only place the package reads the
wall clock. Without this, a stray `datetime.now()` added later would ignore the offset,
and the demo would half-move in time: some rows stamped with the fast-forwarded clock,
others with the real one, and nothing would look obviously wrong until the numbers
stopped adding up on camera.
"""

from __future__ import annotations

import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "chase"

FORBIDDEN = [
    re.compile(r"\bdatetime\.now\("),
    re.compile(r"\bdatetime\.utcnow\("),
    re.compile(r"\bdate\.today\("),
    re.compile(r"\btime\.time\("),
]


def test_no_wall_clock_reads_outside_clock_module():
    offenders: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.name == "clock.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            for pattern in FORBIDDEN:
                if pattern.search(code):
                    offenders.append(f"{path.relative_to(PACKAGE.parent)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "the wall clock is read outside chase/clock.py, so these call sites would ignore "
        "CHASE_CLOCK_OFFSET:\n  " + "\n  ".join(offenders)
    )


def test_clock_module_itself_does_read_the_wall_clock():
    """Guards against the grep passing because the real call was deleted."""
    source = (PACKAGE / "clock.py").read_text(encoding="utf-8")
    assert "datetime.now(timezone.utc)" in source
