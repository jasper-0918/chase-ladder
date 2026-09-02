"""One pass of the ladder. Spec section 4.5.

The run takes its collaborators as arguments (a HubSpot client, a mail poller, a sender,
an opener). Offline tests pass fakes, so the whole decision path is testable with no
network and no mocking library, and the live wiring is one call site in the CLI.

Order matters and is the design:

    1. load config              6. window check, before anything is claimed
    2. search candidates        7. direct read of due deals, freshest stage wins
    3. poll replies -> stops    8. claim, then send, then mark
    4. work out what is due     9. project to HubSpot
    5. repair the projection   10. count what happened
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol, Sequence

from chase import db
from chase.config import Client
from chase.db import Claim
from chase.ladder import Candidate, due_step, is_ladder_exhausted
from chase.mailer import RefusedBeforeDelivery
from chase.report import RunReport
from chase.window import in_send_window

DEFAULT_UNRESOLVED_AFTER = timedelta(minutes=5)


class HubSpot(Protocol):
    def search_candidates(self, client: Client) -> list[Candidate]: ...
    def read_deal(self, deal_id: str) -> Candidate: ...
    def patch_after_send(self, deal_id: str, last_chase_at: datetime, stage: str | None) -> None: ...
    def patch_stage(self, deal_id: str, stage: str) -> None: ...


class Mail(Protocol):
    def poll_replies(self) -> list[dict]: ...
    def send(self, message) -> None: ...


@dataclass
class Deps:
    hubspot: HubSpot
    mail: Mail
    build_message: Callable
    opener: Callable
    make_message_id: Callable
    unresolved_after: timedelta = DEFAULT_UNRESOLVED_AFTER


class RunAborted(RuntimeError):
    """A read that would not answer. Nothing was claimed; the caller reports 500."""


def run_ladder(
    conn,
    client: Client,
    now: datetime,
    deps: Deps,
    clock_offset: str = "0d",
    dry: bool = False,
) -> RunReport:
    report = RunReport(client=client.client, now=now, clock_offset=clock_offset, _dry=dry)

    # 2. Candidates -----------------------------------------------------------
    try:
        candidates: Sequence[Candidate] = deps.hubspot.search_candidates(client)
    except Exception as exc:  # noqa: BLE001
        raise RunAborted(f"HubSpot search failed: {exc}") from exc
    report.candidates = len(candidates)
    by_id = {c.deal_id: c for c in candidates}

    # 3. Replies, the correctness path ----------------------------------------
    try:
        replies = deps.mail.poll_replies()
    except Exception as exc:  # noqa: BLE001
        # Chasing without the ability to see replies is the one thing this design
        # refuses to do, so a poll failure ends the run before anything is claimed.
        raise RunAborted(f"reply poll failed: {exc}") from exc

    known_ids = {row["deal_id"] for row in db.all_log_rows(conn)} | set(by_id)
    for reply in replies:
        deal_id = reply.get("deal_id")
        if deal_id and deal_id in known_ids:
            db.record_stop(conn, deal_id, "reply", reply["message_id"], now=now)
        else:
            report.unmatched_replies += 1

    stopped_ids = db.stopped_deal_ids(conn)

    # 4. What is due ----------------------------------------------------------
    log_by_deal: dict[str, list] = {}
    for row in db.all_log_rows(conn):
        log_by_deal.setdefault(row["deal_id"], []).append(row)

    due: list[tuple[Candidate, object]] = []
    for candidate in candidates:
        rows = log_by_deal.get(candidate.deal_id, [])
        if is_ladder_exhausted(client, rows) and candidate.deal_id not in stopped_ids:
            report.ladder_exhausted += 1
        step = due_step(candidate, client.ladder, now, rows)
        if step is not None:
            due.append((candidate, step))
    report.due = len(due)

    # 5. Repair the Replied projection ---------------------------------------
    if not dry:
        for deal_id in stopped_ids:
            candidate = by_id.get(deal_id)
            if candidate is None or candidate.stage not in ("Sent", "Chasing"):
                continue
            if any(r["reason"] == "reply" for r in _stop_rows(conn, deal_id)):
                try:
                    deps.hubspot.patch_stage(deal_id, "Replied")
                except Exception:  # noqa: BLE001
                    report.hubspot_sync_failed += 1

    # 6. Send window ----------------------------------------------------------
    if not in_send_window(now, client.send_window):
        report.skipped_window = len(due)
        report.unresolved = len(db.unresolved_rows(conn, now, deps.unresolved_after))
        return report

    if dry:
        report.unresolved = len(db.unresolved_rows(conn, now, deps.unresolved_after))
        return report

    # 7 and 8. Fresh stage, then claim and send -------------------------------
    for candidate, step in due:
        try:
            fresh = deps.hubspot.read_deal(candidate.deal_id)
        except Exception as exc:  # noqa: BLE001
            raise RunAborted(f"HubSpot read failed for {candidate.deal_id}: {exc}") from exc

        if fresh.stage in client.stop_on_stages:
            db.record_stop(conn, candidate.deal_id, "stage", f"stage:{fresh.stage.lower()}", now=now)
            report.stopped += 1
            continue
        if fresh.stage not in ("Sent", "Chasing"):
            # Replied, or dragged back to Draft. Not ours this run, and nothing written.
            continue

        outcome = _send_one(conn, client, fresh, step, now, deps, report)
        if outcome is Claim.SENT:
            report.sent += 1
        elif outcome is Claim.FAILED:
            report.failed += 1
        elif outcome is Claim.STOPPED:
            report.stopped += 1

    # 10. Count ---------------------------------------------------------------
    report.unresolved = len(db.unresolved_rows(conn, now, deps.unresolved_after))
    return report


def _stop_rows(conn, deal_id: str) -> list:
    return list(conn.execute("SELECT * FROM stops WHERE deal_id = ?", (deal_id,)))


def _send_one(conn, client, candidate, step, now, deps: Deps, report: RunReport):
    """Claim, then send, then mark. The claim is the only thing that decides whether a
    message may go out; everything after it is bookkeeping."""
    message_id = deps.make_message_id(client)
    outcome = db.claim(conn, candidate.deal_id, step.step, message_id, now=now)
    if outcome is not Claim.CLAIMED:
        return outcome

    opening_line, from_model = deps.opener(client, candidate, step)
    if not from_model and step.opener == "groq":
        report.groq_fallbacks += 1
    message = deps.build_message(client, candidate, step, opening_line, now, message_id)

    try:
        deps.mail.send(message)
    except RefusedBeforeDelivery:
        # The server took nothing. This rung is retryable on the next run.
        db.mark(conn, candidate.deal_id, step.step, "failed")
        return Claim.FAILED
    except Exception:  # noqa: BLE001
        # Ambiguous: the server may already hold the message. The row stays `claimed`,
        # which nothing ever retries, and a human settles it from Mailpit.
        return Claim.UNKNOWN

    db.mark(conn, candidate.deal_id, step.step, "sent", sent_at=now)
    try:
        deps.hubspot.patch_after_send(
            candidate.deal_id,
            last_chase_at=now,
            stage="Chasing" if candidate.stage == "Sent" else None,
        )
    except Exception:  # noqa: BLE001
        report.hubspot_sync_failed += 1
    return Claim.SENT
