"""The run report. Spec section 6.

Every counter here exists to expose a specific silent failure. A run that reports
`ok, 0 sent` should never be ambiguous: either the window was closed, or nothing was
due, or the seed rotted, and the counters say which.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime


@dataclass
class RunReport:
    client: str
    now: datetime
    clock_offset: str
    candidates: int = 0
    due: int = 0
    sent: int = 0
    skipped_window: int = 0
    stopped: int = 0
    unresolved: int = 0
    failed: int = 0
    hubspot_sync_failed: int = 0
    groq_fallbacks: int = 0
    unmatched_replies: int = 0
    ladder_exhausted: int = 0
    error: str | None = None
    _dry: bool = field(default=False, repr=False)

    @property
    def status(self) -> str:
        """`failed` whenever a rung failed or a claim is unresolved. The n8n IF branch
        keys on exactly this string, so it never becomes a judgement call."""
        return "failed" if (self.failed + self.unresolved) > 0 else "ok"

    def to_dict(self) -> dict:
        data = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        data["now"] = self.now.isoformat()
        data["status"] = self.status
        if self._dry:
            data["dry"] = True
        return data

    def summary_line(self) -> str:
        return (
            f"{self.client}: candidates {self.candidates}, due {self.due}, sent {self.sent}, "
            f"stopped {self.stopped}, skipped_window {self.skipped_window}, "
            f"failed {self.failed}, unresolved {self.unresolved}, "
            f"exhausted {self.ladder_exhausted}, status {self.status}"
        )


def aborted(client: str, now: datetime, clock_offset: str, message: str) -> RunReport:
    """A run that never reached the ladder: a HubSpot read that would not answer, a
    Mailpit poll that failed, a broken config. Nothing was claimed, and the route turns
    this into an HTTP 500 so n8n takes its error path."""
    report = RunReport(client=client, now=now, clock_offset=clock_offset, error=message)
    report.failed = 0
    report.unresolved = 0
    return report
