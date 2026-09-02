"""Spec sections 4.5 and 6: what a run does and what it counts.

These tests cover the ORCHESTRATION, so they isolate it from the two functions Jasper
writes by hand. Both are replaced here by deliberately simplified doubles:

  * `simple_claim` sequences the run correctly but has none of the transaction
    discipline the real one needs. It is not an implementation, and it would fail every
    interesting test in tests/test_claim.py.
  * `window_says` is a switch, not a timezone calculation.

Correctness of those two lives in tests/test_claim.py and tests/test_window.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from chase import db, run as run_module
from chase.config import load_client
from chase.db import Claim
from chase.ladder import Candidate
from chase.mailer import RefusedBeforeDelivery
from chase.run import Deps, RunAborted, run_ladder

NOW = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)  # a Tuesday, 13:00 in Sydney


# --------------------------------------------------------------------------- doubles
def simple_claim(conn, deal_id, step, message_id, now=None):
    """A test double. NOT an implementation: no BEGIN IMMEDIATE, no ON CONFLICT."""
    if db.is_stopped(conn, deal_id):
        return Claim.STOPPED
    existing = conn.execute(
        "SELECT status FROM reminder_log WHERE deal_id = ? AND step = ?", (deal_id, step)
    ).fetchone()
    if existing and existing["status"] in ("claimed", "sent"):
        return Claim.ALREADY_LOGGED
    stamp = (now or NOW).isoformat()
    if existing:
        conn.execute(
            "UPDATE reminder_log SET status='claimed', claimed_at=?, message_id=?, sent_at=NULL "
            "WHERE deal_id=? AND step=?",
            (stamp, message_id, deal_id, step),
        )
    else:
        conn.execute(
            "INSERT INTO reminder_log (deal_id, step, status, claimed_at, message_id) "
            "VALUES (?, ?, 'claimed', ?, ?)",
            (deal_id, step, stamp, message_id),
        )
    return Claim.CLAIMED


class FakeHubSpot:
    def __init__(self, candidates, fresh_stages=None, fail_search=False, fail_patch=False):
        self._candidates = candidates
        self._fresh = fresh_stages or {}
        self.fail_search = fail_search
        self.fail_patch = fail_patch
        self.patched_after_send: list[str] = []
        self.patched_stage: list[tuple[str, str]] = []

    def search_candidates(self, client):
        if self.fail_search:
            raise RuntimeError("429 from HubSpot")
        return list(self._candidates)

    def read_deal(self, deal_id):
        base = next(c for c in self._candidates if c.deal_id == deal_id)
        stage = self._fresh.get(deal_id, base.stage)
        return Candidate(base.deal_id, base.name, base.amount, stage, base.quote_sent_at,
                         base.last_chase_at, base.contact_email, base.contact_name)

    def patch_after_send(self, deal_id, last_chase_at, stage):
        if self.fail_patch:
            raise RuntimeError("PATCH failed")
        self.patched_after_send.append(deal_id)

    def patch_stage(self, deal_id, stage):
        if self.fail_patch:
            raise RuntimeError("PATCH failed")
        self.patched_stage.append((deal_id, stage))


class FakeMail:
    def __init__(self, replies=None, fail_poll=False, raise_on_send=None):
        self._replies = replies or []
        self.fail_poll = fail_poll
        self.raise_on_send = raise_on_send
        self.sent = []

    def poll_replies(self):
        if self.fail_poll:
            raise RuntimeError("Mailpit unreachable")
        return list(self._replies)

    def send(self, message):
        if self.raise_on_send:
            raise self.raise_on_send
        self.sent.append(message)


def quote(deal_id, days_old, stage="Sent", amount=4200.0):
    return Candidate(
        deal_id=deal_id,
        name=f"Q-04{deal_id} website rebuild, Customer {deal_id}",
        amount=amount,
        stage=stage,
        quote_sent_at=NOW - timedelta(days=days_old),
        contact_email=f"c{deal_id}@customer.example",
    )


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "data" / "chase.db")
    db.init(c)
    yield c
    c.close()


@pytest.fixture
def client(config_dir):
    return load_client("harbourline", config_dir)


@pytest.fixture
def wire(monkeypatch, tmp_path):
    """Install the doubles and return a builder for Deps."""
    monkeypatch.setattr(db, "claim", simple_claim)
    monkeypatch.setattr(run_module.db, "claim", simple_claim)

    def _wire(hubspot, mail, window_open=True, opener=None):
        monkeypatch.setattr(run_module, "in_send_window", lambda now, w: window_open)
        return Deps(
            hubspot=hubspot,
            mail=mail,
            build_message=lambda *a, **k: {"to": "x"},
            opener=opener or (lambda c, cand, step: ("Hope the week is treating you well.", False)),
            make_message_id=lambda c: f"<{id(c)}@test>",
        )

    return _wire


# --------------------------------------------------------------------------- tests
def test_a_run_sends_the_due_rungs_and_counts_them(conn, client, wire):
    candidates = [quote("1", 0), quote("2", 3), quote("3", 8), quote("4", 20)]
    mail = FakeMail()
    report = run_ladder(conn, client, NOW, wire(FakeHubSpot(candidates), mail))
    assert report.candidates == 4
    assert report.due == 3          # ages 3, 8 and 20 are due; age 0 is not
    assert report.sent == 3
    assert len(mail.sent) == 3
    assert report.status == "ok"


def test_a_second_run_adds_no_rows(conn, client, wire):
    """The beat filmed on camera. Spec section 13 beat 3."""
    candidates = [quote("1", 3), quote("2", 8)]
    deps = wire(FakeHubSpot(candidates), FakeMail())
    first = run_ladder(conn, client, NOW, deps)
    before = db.counts(conn)["reminder_log"]
    second = run_ladder(conn, client, NOW, deps)
    assert first.sent == 2
    assert second.sent == 0 and second.due == 0
    assert db.counts(conn)["reminder_log"] == before


def test_a_reply_stops_the_ladder_and_is_recorded_once(conn, client, wire):
    replies = [{"deal_id": "1", "message_id": "<r1@customer>"}]
    mail = FakeMail(replies=replies)
    hubspot = FakeHubSpot([quote("1", 3), quote("2", 3)])
    report = run_ladder(conn, client, NOW, wire(hubspot, mail))
    assert db.is_stopped(conn, "1") is True
    assert report.sent == 1, "only the deal without a reply is chased"
    assert conn.execute("SELECT COUNT(*) FROM stops").fetchone()[0] == 1
    # The same reply seen again on the next run does not add a second stop row.
    run_ladder(conn, client, NOW, wire(hubspot, FakeMail(replies=replies)))
    assert conn.execute("SELECT COUNT(*) FROM stops").fetchone()[0] == 1


def test_a_reply_projects_replied_back_to_hubspot(conn, client, wire):
    hubspot = FakeHubSpot([quote("1", 3)])
    mail = FakeMail(replies=[{"deal_id": "1", "message_id": "<r1@customer>"}])
    run_ladder(conn, client, NOW, wire(hubspot, mail))
    assert ("1", "Replied") in hubspot.patched_stage


def test_an_unmatched_reply_is_counted_not_guessed(conn, client, wire):
    mail = FakeMail(replies=[{"deal_id": "999999", "message_id": "<r@stranger>"}])
    report = run_ladder(conn, client, NOW, wire(FakeHubSpot([quote("1", 3)]), mail))
    assert report.unmatched_replies == 1
    assert db.is_stopped(conn, "999999") is False


def test_a_deal_moved_to_won_is_stopped_before_any_claim(conn, client, wire):
    """The owner closed it by hand. The direct read catches that even though the search
    index still says Sent. Spec section 13 beat 5."""
    hubspot = FakeHubSpot([quote("1", 3), quote("2", 3)], fresh_stages={"1": "Won"})
    mail = FakeMail()
    report = run_ladder(conn, client, NOW, wire(hubspot, mail))
    assert report.stopped == 1
    assert report.sent == 1
    assert db.is_stopped(conn, "1") is True
    assert db.log_rows_for(conn, "1") == [], "a stopped deal is never claimed"


def test_a_deal_found_replied_is_dropped_with_no_row(conn, client, wire):
    hubspot = FakeHubSpot([quote("1", 3)], fresh_stages={"1": "Replied"})
    report = run_ladder(conn, client, NOW, wire(hubspot, FakeMail()))
    assert report.sent == 0 and report.stopped == 0
    assert db.log_rows_for(conn, "1") == []
    assert db.is_stopped(conn, "1") is False


def test_closed_window_claims_nothing(conn, client, wire):
    report = run_ladder(
        conn, client, NOW, wire(FakeHubSpot([quote("1", 3), quote("2", 8)]), FakeMail(), window_open=False)
    )
    assert report.due == 2 and report.skipped_window == 2 and report.sent == 0
    assert report.status == "ok", "a closed window is not a failure"
    assert db.counts(conn)["reminder_log"] == 0


def test_a_reply_still_stops_the_ladder_outside_the_window(conn, client, wire):
    """The poll runs before the window check, so a reply that arrives overnight is
    recorded even though nothing is sent."""
    mail = FakeMail(replies=[{"deal_id": "1", "message_id": "<r@c>"}])
    run_ladder(conn, client, NOW, wire(FakeHubSpot([quote("1", 3)]), mail, window_open=False))
    assert db.is_stopped(conn, "1") is True


def test_a_refused_send_is_failed_and_retryable(conn, client, wire):
    hubspot = FakeHubSpot([quote("1", 3)])
    mail = FakeMail(raise_on_send=RefusedBeforeDelivery("connection refused"))
    report = run_ladder(conn, client, NOW, wire(hubspot, mail))
    assert report.failed == 1 and report.sent == 0
    assert report.status == "failed"
    assert db.log_rows_for(conn, "1")[0]["status"] == "failed"
    # The next run, with the server back, retries that rung.
    ok = run_ladder(conn, client, NOW, wire(hubspot, FakeMail()))
    assert ok.sent == 1
    assert db.log_rows_for(conn, "1")[0]["status"] == "sent"


def test_an_ambiguous_send_leaves_the_row_claimed_and_never_retries(conn, client, wire):
    """The one case that could deliver twice, so it never auto-retries. Spec 4.4."""
    hubspot = FakeHubSpot([quote("1", 3)])
    mail = FakeMail(raise_on_send=TimeoutError("no response after DATA"))
    report = run_ladder(conn, client, NOW, wire(hubspot, mail))
    assert report.failed == 0 and report.sent == 0
    assert db.log_rows_for(conn, "1")[0]["status"] == "claimed"
    later = run_ladder(conn, client, NOW, wire(hubspot, FakeMail()))
    assert later.sent == 0, "an ambiguous rung is never resent"
    assert later.due == 0


def test_an_old_claimed_row_becomes_unresolved_and_fails_the_run(conn, client, wire):
    hubspot = FakeHubSpot([quote("1", 3)])
    run_ladder(conn, client, NOW, wire(hubspot, FakeMail(raise_on_send=TimeoutError("x"))))
    later = run_ladder(conn, client, NOW + timedelta(minutes=30), wire(hubspot, FakeMail()))
    assert later.unresolved == 1
    assert later.status == "failed"


def test_a_failed_hubspot_patch_is_counted_not_raised(conn, client, wire):
    hubspot = FakeHubSpot([quote("1", 3)], fail_patch=True)
    report = run_ladder(conn, client, NOW, wire(hubspot, FakeMail()))
    assert report.sent == 1, "the send stands even when the projection fails"
    assert report.hubspot_sync_failed == 1
    assert report.status == "ok"
    assert db.log_rows_for(conn, "1")[0]["status"] == "sent"


def test_a_search_failure_aborts_before_anything_is_claimed(conn, client, wire):
    with pytest.raises(RunAborted, match="HubSpot search failed"):
        run_ladder(conn, client, NOW, wire(FakeHubSpot([], fail_search=True), FakeMail()))
    assert db.counts(conn)["reminder_log"] == 0


def test_a_poll_failure_aborts_rather_than_chasing_blind(conn, client, wire):
    """Chasing without the ability to see replies is the one thing this refuses to do."""
    with pytest.raises(RunAborted, match="reply poll failed"):
        run_ladder(conn, client, NOW, wire(FakeHubSpot([quote("1", 3)]), FakeMail(fail_poll=True)))
    assert db.counts(conn)["reminder_log"] == 0


def test_ladder_exhausted_is_counted(conn, client, wire):
    hubspot = FakeHubSpot([quote("1", 30)])
    deps = wire(hubspot, FakeMail())
    run_ladder(conn, client, NOW, deps)                    # sends step 3, the last rung
    report = run_ladder(conn, client, NOW, deps)
    assert report.ladder_exhausted == 1 and report.due == 0


def test_groq_fallback_is_counted_on_step_one(conn, client, wire):
    hubspot = FakeHubSpot([quote("1", 3)])
    deps = wire(hubspot, FakeMail(), opener=lambda c, cand, s: ("templated", False))
    report = run_ladder(conn, client, NOW, deps)
    assert report.groq_fallbacks == 1


def test_a_model_written_opener_is_not_counted_as_a_fallback(conn, client, wire):
    hubspot = FakeHubSpot([quote("1", 3)])
    deps = wire(hubspot, FakeMail(), opener=lambda c, cand, s: ("from the model", True))
    report = run_ladder(conn, client, NOW, deps)
    assert report.groq_fallbacks == 0


def test_a_dry_pass_writes_nothing(conn, client, wire):
    """`status` and `digest` read the world without touching it."""
    hubspot = FakeHubSpot([quote("1", 3), quote("2", 8)])
    mail = FakeMail()
    report = run_ladder(conn, client, NOW, wire(hubspot, mail), dry=True)
    assert report.due == 2 and report.sent == 0
    assert db.counts(conn)["reminder_log"] == 0
    assert mail.sent == []
    assert hubspot.patched_after_send == []


def test_a_dry_pass_still_records_a_reply_stop(conn, client, wire):
    """The poll is the correctness path, so even a read-only pass may not throw a reply
    away. Spec section 4.1."""
    mail = FakeMail(replies=[{"deal_id": "1", "message_id": "<r@c>"}])
    run_ladder(conn, client, NOW, wire(FakeHubSpot([quote("1", 3)]), mail), dry=True)
    assert db.is_stopped(conn, "1") is True


def test_report_counters_are_consistent(conn, client, wire):
    hubspot = FakeHubSpot([quote(str(i), 3 + i) for i in range(1, 6)], fresh_stages={"2": "Won"})
    report = run_ladder(conn, client, NOW, wire(hubspot, FakeMail()))
    assert report.sent + report.failed + report.stopped + report.skipped_window <= report.due
    data = report.to_dict()
    for key in (
        "client", "now", "clock_offset", "candidates", "due", "sent", "skipped_window",
        "stopped", "unresolved", "failed", "hubspot_sync_failed", "groq_fallbacks",
        "unmatched_replies", "ladder_exhausted", "status", "error",
    ):
        assert key in data, f"the run report must always carry {key}"
    assert data["error"] is None
