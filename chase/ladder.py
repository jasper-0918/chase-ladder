"""Which rung is due, and why. Spec section 4.5 step 4.

Pure functions over (quote_sent_at, ladder, now, log rows). No database, no network, no
clock: everything is an argument, so each of these can be tested against a single known
record, which is the definition of small enough to reason about.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Sequence

from chase.config import Client, Step

BLOCKING_STATUSES = ("claimed", "sent")


@dataclass(frozen=True)
class Candidate:
    """One quote as the ladder sees it, assembled from a HubSpot search result."""

    deal_id: str
    name: str
    amount: float
    stage: str
    quote_sent_at: datetime
    last_chase_at: datetime | None = None
    contact_email: str | None = None
    contact_name: str | None = None


def highest_blocking_step(log_rows: Iterable) -> int:
    """The highest step already claimed or sent for a deal.

    A `failed` row does not block: the server refused that rung outright, so it is
    retryable. A `claimed` row does block, permanently, because its outcome is unknown
    and resending could deliver twice. Spec section 4.4.
    """
    steps = [
        int(row["step"])
        for row in log_rows
        if row["status"] in BLOCKING_STATUSES
    ]
    return max(steps, default=0)


def due_step(
    candidate: Candidate,
    ladder: Sequence[Step],
    now: datetime,
    log_rows: Iterable,
) -> Step | None:
    """The one step to send for this candidate right now, or None.

    Two rules, and the second is the one people get wrong:

      * a step is eligible when `quote_sent_at + days_after_sent` has passed and its
        number is above the highest blocking step;
      * the HIGHEST eligible step is the one that goes out, and lower unsent steps are
        skipped for good. A quote that is ten days old on the day the system is switched
        on gets one message, not three inside a minute.
    """
    blocking = highest_blocking_step(log_rows)
    eligible = [
        step
        for step in ladder
        if step.step > blocking
        and candidate.quote_sent_at + timedelta(days=step.days_after_sent) <= now
    ]
    return max(eligible, key=lambda s: s.step) if eligible else None


def is_ladder_exhausted(client: Client, log_rows: Iterable) -> bool:
    """True when the last rung has been claimed or sent, so the system is done here and
    the quote is the owner's to phone about. Spec section 6."""
    return highest_blocking_step(log_rows) >= client.last_step


def age_in_days(candidate: Candidate, now: datetime) -> float:
    return (now - candidate.quote_sent_at).total_seconds() / 86400.0
