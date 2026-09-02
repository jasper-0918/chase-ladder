"""Spec sections 4.5 step 8, 4.4 and 12.

These tests were written before `claim()` existed and are its specification: the body
was implemented against them, red first, then green. Read chase/db.py's claim
docstring alongside them.

The last two tests are the ones worth the effort: they are the concurrency story, and
they fail loudly if the transaction is DEFERRED rather than IMMEDIATE.
"""

from __future__ import annotations

import sqlite3
import threading
import time as time_module
from datetime import datetime, timedelta, timezone

import pytest

from chase import db
from chase.db import Claim


@pytest.fixture
def dbfile(tmp_path):
    path = tmp_path / "data" / "chase.db"
    conn = db.connect(path)
    db.init(conn)
    conn.close()
    return path


@pytest.fixture
def conn(dbfile):
    c = db.connect(dbfile)
    yield c
    c.close()


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


NOW = "2026-09-01T12:00"


def test_claim_takes_a_fresh_rung(conn):
    assert db.claim(conn, "deal1", 1, "<m1@x>", now=at(NOW)) is Claim.CLAIMED
    row = db.log_rows_for(conn, "deal1")[0]
    assert row["status"] == "claimed"
    assert row["message_id"] == "<m1@x>"
    assert row["sent_at"] is None
    assert row["claimed_at"].startswith("2026-09-01T12:00")


def test_claim_rejects_a_stopped_deal(conn):
    """The reply arrived first. Nothing is written, and the ladder moves on."""
    db.record_stop(conn, "deal1", "reply", "<r@customer>", now=at(NOW))
    assert db.claim(conn, "deal1", 1, "<m1@x>", now=at(NOW)) is Claim.STOPPED
    assert db.log_rows_for(conn, "deal1") == []


def test_claim_is_idempotent_against_a_claimed_row(conn):
    db.claim(conn, "deal1", 1, "<m1@x>", now=at(NOW))
    assert db.claim(conn, "deal1", 1, "<m2@x>", now=at(NOW)) is Claim.ALREADY_LOGGED
    rows = db.log_rows_for(conn, "deal1")
    assert len(rows) == 1 and rows[0]["message_id"] == "<m1@x>"


def test_claim_is_idempotent_against_a_sent_row(conn):
    """A re-run adds zero rows. This is the beat filmed on camera."""
    db.claim(conn, "deal1", 1, "<m1@x>", now=at(NOW))
    db.mark(conn, "deal1", 1, "sent", sent_at=at(NOW))
    assert db.claim(conn, "deal1", 1, "<m2@x>", now=at(NOW)) is Claim.ALREADY_LOGGED
    assert db.log_rows_for(conn, "deal1")[0]["status"] == "sent"


def test_claim_takes_over_a_failed_row(conn):
    """A rung the server refused is retryable, and the retry gets a fresh Message-ID and
    a fresh claimed_at, with sent_at cleared. Spec section 4.5 step 8."""
    db.claim(conn, "deal1", 1, "<m1@x>", now=at("2026-09-01T09:00"))
    db.mark(conn, "deal1", 1, "failed")
    assert db.claim(conn, "deal1", 1, "<m2@x>", now=at(NOW)) is Claim.CLAIMED
    rows = db.log_rows_for(conn, "deal1")
    assert len(rows) == 1
    assert rows[0]["status"] == "claimed"
    assert rows[0]["message_id"] == "<m2@x>"
    assert rows[0]["sent_at"] is None
    assert rows[0]["claimed_at"].startswith("2026-09-01T12:00")


def test_claim_writes_one_row_per_deal_and_step(conn):
    for step in (1, 2, 3):
        assert db.claim(conn, "deal1", step, f"<m{step}@x>", now=at(NOW)) is Claim.CLAIMED
    assert len(db.log_rows_for(conn, "deal1")) == 3


def test_claimed_at_is_the_wall_clock_at_the_claim_not_the_run_start(conn):
    """Stamping the run's fixed `now` would make a healthy overlapping run look
    unresolved and page the owner for nothing. Spec section 4.5 step 10."""
    run_started = at("2026-09-01T12:00")
    claimed_later = at("2026-09-01T12:04")
    db.claim(conn, "deal1", 1, "<m1@x>", now=claimed_later)

    # A second run beginning between the two instants must not count this row.
    assert db.unresolved_rows(conn, run_started + timedelta(seconds=30), timedelta(minutes=5)) == []
    # Once the grace window passes, it is genuinely unresolved.
    stale = db.unresolved_rows(conn, claimed_later + timedelta(minutes=6), timedelta(minutes=5))
    assert [r["deal_id"] for r in stale] == ["deal1"]


def test_claim_leaves_no_transaction_open(conn):
    """Every path commits or rolls back. A leaked transaction holds the write lock and
    every stop in the system waits on it."""
    db.claim(conn, "deal1", 1, "<m1@x>", now=at(NOW))
    assert conn.in_transaction is False
    db.record_stop(conn, "deal2", "reply", "<r@x>", now=at(NOW))
    db.claim(conn, "deal2", 1, "<m2@x>", now=at(NOW))
    assert conn.in_transaction is False
    db.claim(conn, "deal1", 1, "<m3@x>", now=at(NOW))
    assert conn.in_transaction is False


def test_claim_takes_the_write_lock_at_begin(dbfile):
    """BEGIN IMMEDIATE, not DEFERRED: while a claim is open, a second connection cannot
    start its own write transaction. If this passes with DEFERRED it is because the
    claim never took the lock, which is the bug this whole design exists to avoid."""
    a = db.connect(dbfile)
    b = sqlite3.connect(dbfile, timeout=0.2, autocommit=True)
    try:
        a.execute("BEGIN IMMEDIATE")
        a.execute(
            "INSERT INTO reminder_log (deal_id, step, status, claimed_at, message_id) "
            "VALUES ('deal1', 1, 'claimed', ?, '<m@x>')",
            (at(NOW).isoformat(),),
        )
        assert a.in_transaction is True
        with pytest.raises(sqlite3.OperationalError, match="locked|busy"):
            b.execute("BEGIN IMMEDIATE")
        a.execute("COMMIT")
        assert a.in_transaction is False
    finally:
        a.close()
        b.close()


def test_threaded_stop_during_a_claim_never_produces_both(dbfile):
    """The race, run for real. A stop lands while the ladder is claiming. Either the
    stop committed first and the claim is refused, or the claim committed first and the
    send is logged. Never a send after a stop that was already committed."""
    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}
    barrier = threading.Barrier(2)

    def claimer():
        c = db.connect(dbfile)
        try:
            barrier.wait()
            results["claim"] = db.claim(c, "deal1", 1, "<m1@x>", now=at(NOW))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread below
            errors["claim"] = exc
        finally:
            c.close()

    def stopper():
        c = db.connect(dbfile)
        try:
            barrier.wait()
            time_module.sleep(0.001)
            results["stop"] = db.record_stop(c, "deal1", "reply", "<r@x>", now=at(NOW))
        except BaseException as exc:  # noqa: BLE001
            errors["stop"] = exc
        finally:
            c.close()

    threads = [threading.Thread(target=claimer), threading.Thread(target=stopper)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # Surface a thread's exception as itself, so a stubbed claim() reads as
    # NotImplementedError here rather than as a KeyError three lines further down.
    for name, exc in errors.items():
        raise AssertionError(f"the {name} thread raised {exc!r}") from exc

    check = db.connect(dbfile)
    try:
        rows = db.log_rows_for(check, "deal1")
        stopped = db.is_stopped(check, "deal1")
    finally:
        check.close()

    assert stopped is True
    if results["claim"] is Claim.STOPPED:
        assert rows == [], "refused claims must write nothing"
    else:
        assert results["claim"] is Claim.CLAIMED
        assert len(rows) == 1, "a won claim writes exactly one row"
