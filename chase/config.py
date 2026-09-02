"""Client configuration. Spec section 4.3.

One YAML file is one client. Onboarding a business is a file plus an `.env`, never a
code change, so everything that differs between businesses lives here: the ladder, the
sending hours, the tone, the work categories the model may see, the digest schedule.

The loader validates on load. A missing key fails at start rather than at 03:00 on the
first scheduled run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any

import yaml

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
OPENERS = {"groq", "template"}


class ConfigError(ValueError):
    """Raised for any malformed client file, with the file name and the key at fault."""


@dataclass(frozen=True)
class Step:
    step: int
    days_after_sent: int
    subject: str
    template: str
    opener: str


@dataclass(frozen=True)
class SendWindow:
    days: tuple[str, ...]
    start: time
    end: time
    tz: str


@dataclass(frozen=True)
class Digest:
    day: str
    time: time
    stale_after_days: int


@dataclass(frozen=True)
class Client:
    client: str
    business_name: str
    city: str
    currency: str
    from_name: str
    from_address: str
    reply_domain: str
    tone: str
    work_categories: tuple[str, ...]
    ladder: tuple[Step, ...]
    send_window: SendWindow
    stop_on_stages: tuple[str, ...]
    digest: Digest

    @property
    def last_step(self) -> int:
        return self.ladder[-1].step

    def step(self, number: int) -> Step:
        for candidate in self.ladder:
            if candidate.step == number:
                return candidate
        raise KeyError(f"{self.client} has no step {number}")


def _require(data: dict[str, Any], key: str, where: str) -> Any:
    if key not in data:
        raise ConfigError(f"{where}: missing required key {key!r}")
    return data[key]


def _parse_hhmm(value: Any, where: str) -> time:
    if not isinstance(value, str):
        raise ConfigError(f"{where}: expected a quoted HH:MM string, got {value!r}")
    try:
        hours, minutes = value.split(":")
        return time(int(hours), int(minutes))
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{where}: cannot read {value!r} as HH:MM") from exc


def load_client(name: str, config_dir: Path | str = "config") -> Client:
    path = Path(config_dir) / f"{name}.yaml"
    if not path.exists():
        raise ConfigError(f"no client file at {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    where = str(path)

    ladder_raw = _require(raw, "ladder", where)
    if not ladder_raw:
        raise ConfigError(f"{where}: ladder is empty, so nothing would ever be chased")
    steps: list[Step] = []
    for entry in ladder_raw:
        number = _require(entry, "step", f"{where} ladder entry")
        opener = _require(entry, "opener", f"{where} step {number}")
        if opener not in OPENERS:
            raise ConfigError(
                f"{where} step {number}: opener {opener!r} is not one of {sorted(OPENERS)}"
            )
        steps.append(
            Step(
                step=int(number),
                days_after_sent=int(_require(entry, "days_after_sent", f"{where} step {number}")),
                subject=_require(entry, "subject", f"{where} step {number}"),
                template=_require(entry, "template", f"{where} step {number}"),
                opener=opener,
            )
        )
    numbers = [s.step for s in steps]
    if numbers != sorted(numbers) or len(set(numbers)) != len(numbers):
        raise ConfigError(f"{where}: ladder steps must be unique and ascending, got {numbers}")
    days_after = [s.days_after_sent for s in steps]
    if days_after != sorted(days_after) or len(set(days_after)) != len(days_after):
        raise ConfigError(
            f"{where}: days_after_sent must increase with the step, got {days_after}"
        )

    window_raw = _require(raw, "send_window", where)
    window_days = tuple(str(d).lower() for d in _require(window_raw, "days", f"{where} send_window"))
    unknown = [d for d in window_days if d not in DAYS]
    if unknown:
        raise ConfigError(f"{where} send_window: unknown day names {unknown}, expected {DAYS}")
    window = SendWindow(
        days=window_days,
        start=_parse_hhmm(_require(window_raw, "start", f"{where} send_window"), f"{where} send_window.start"),
        end=_parse_hhmm(_require(window_raw, "end", f"{where} send_window"), f"{where} send_window.end"),
        tz=_require(window_raw, "tz", f"{where} send_window"),
    )
    if window.start >= window.end:
        raise ConfigError(
            f"{where} send_window: start {window.start} is not before end {window.end}; "
            "a window that wraps midnight is not supported and would silently send nothing"
        )

    digest_raw = _require(raw, "digest", where)
    digest_day = str(_require(digest_raw, "day", f"{where} digest")).lower()
    if digest_day not in DAYS:
        raise ConfigError(f"{where} digest: unknown day {digest_day!r}")
    digest = Digest(
        day=digest_day,
        time=_parse_hhmm(_require(digest_raw, "time", f"{where} digest"), f"{where} digest.time"),
        stale_after_days=int(_require(digest_raw, "stale_after_days", f"{where} digest")),
    )

    categories = tuple(_require(raw, "work_categories", where))
    if not categories:
        raise ConfigError(f"{where}: work_categories is empty, so every opener would fall back")

    return Client(
        client=_require(raw, "client", where),
        business_name=_require(raw, "business_name", where),
        city=_require(raw, "city", where),
        currency=_require(raw, "currency", where),
        from_name=_require(raw, "from_name", where),
        from_address=_require(raw, "from_address", where),
        reply_domain=_require(raw, "reply_domain", where),
        tone=_require(raw, "tone", where),
        work_categories=categories,
        ladder=tuple(steps),
        send_window=window,
        stop_on_stages=tuple(_require(_require(raw, "stop_on", where), "stages", f"{where} stop_on")),
        digest=digest,
    )
