"""Spec section 4.4. Everything here except the claim, which lives in test_claim.py."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from chase import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "data" / "chase.db")
    db.init(c)
    yield c
    c.close()


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def insert_claimed(conn, deal_id="1", step=1, claimed_at="2026-09-01T00:00", message_id="<m@x>"):
    conn.execute(
        "INSERT INTO reminder_log (deal_id, step, status, claimed_at, message_id) "
        "VALUES (?, ?, 'claimed', ?, ?)",
        (deal_id, step, at(claimed_at).isoformat(), message_id),
    )


def test_wal_and_busy_timeout_are_set(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_connection_is_autocommit_so_begin_immediate_is_ours(conn):
    """If this regresses to the sqlite3 default, every transaction here silently becomes
    DEFERRED and the claim's race protection is gone. Spec section 4.4."""
    assert conn.autocommit is True
    assert conn.in_transaction is False


def test_unique_deal_step_is_the_at_most_once_artefact(conn):
    insert_claimed(conn)
    with pytest.raises(sqlite3.IntegrityError):
        insert_claimed(conn)


def test_status_is_constrained(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO reminder_log (deal_id, step, status, claimed_at, message_id) "
            "VALUES ('1', 1, 'posted', '2026-09-01T00:00:00+00:00', '<m@x>')"
        )


def test_message_id_is_not_null_so_a_stuck_row_is_findable(conn):
    """A row stuck at `claimed` has to carry the Message-ID its mail would have used,
    or a human cannot search Mailpit for it. Spec section 4.4."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO reminder_log (deal_id, step, status, claimed_at) "
            "VALUES ('1', 1, 'claimed', '2026-09-01T00:00:00+00:00')"
        )


def test_mark_moves_claimed_to_sent(conn):
    insert_claimed(conn)
    db.mark(conn, "1", 1, "sent", sent_at=at("2026-09-01T00:05"))
    row = db.log_rows_for(conn, "1")[0]
    assert row["status"] == "sent" and row["sent_at"].startswith("2026-09-01T00:05")


def test_mark_rejects_a_status_it_must_never_write(conn):
    """`claimed` and `unknown` are not outcomes mark() may write: an ambiguous send
    leaves the row exactly as it is."""
    insert_claimed(conn)
    for status in ("claimed", "unknown", "posted"):
        with pytest.raises(ValueError):
            db.mark(conn, "1", 1, status)


def test_record_stop_adds_one_row_and_reports_it(conn):
    assert db.record_stop(conn, "1", "reply", "<reply@customer>", now=at("2026-09-01T00:00")) is True
    assert db.is_stopped(conn, "1") is True


def test_record_stop_dedupes_on_ref(conn):
    """The webhook and the run-start poll both see the same reply. One stop row."""
    now = at("2026-09-01T00:00")
    assert db.record_stop(conn, "1", "reply", "<same@customer>", now=now) is True
    assert db.record_stop(conn, "1", "reply", "<same@customer>", now=now) is False
    assert conn.execute("SELECT COUNT(*) FROM stops").fetchone()[0] == 1


def test_a_deal_can_carry_several_stops_and_any_one_stops_it(conn):
    now = at("2026-09-01T00:00")
    db.record_stop(conn, "1", "reply", "<r@customer>", now=now)
    db.record_stop(conn, "1", "stage", "stage:won", now=now)
    assert conn.execute("SELECT COUNT(*) FROM stops WHERE deal_id='1'").fetchone()[0] == 2
    assert db.is_stopped(conn, "1") is True


def test_record_stop_rejects_an_unknown_reason(conn):
    """There is no 'paid' reason, because there are no invoices in this system."""
    with pytest.raises(ValueError):
        db.record_stop(conn, "1", "paid", "ref")


def test_manual_stop_fills_its_own_ref(conn):
    db.record_stop(conn, "1", "manual", None, now=at("2026-09-01T00:00"))
    assert db.is_stopped(conn, "1") is True
    ref = conn.execute("SELECT ref FROM stops WHERE deal_id='1'").fetchone()["ref"]
    assert ref.startswith("manual:2026-09-01T00:00")


def test_unresolved_uses_a_grace_window_not_the_run_start(conn):
    """A row claimed seconds ago is healthy even if another run just started; only a row
    older than the grace window is unresolved. Spec section 4.5 step 10."""
    now = at("2026-09-01T12:00")
    insert_claimed(conn, deal_id="fresh", claimed_at="2026-09-01T11:58")   # 2 minutes old
    insert_claimed(conn, deal_id="stale", claimed_at="2026-09-01T11:50")   # 10 minutes old
    unresolved = db.unresolved_rows(conn, now, timedelta(minutes=5))
    assert [r["deal_id"] for r in unresolved] == ["stale"]


def test_unresolved_ignores_sent_and_failed_rows(conn):
    now = at("2026-09-01T12:00")
    insert_claimed(conn, deal_id="a", claimed_at="2026-09-01T10:00")
    insert_claimed(conn, deal_id="b", claimed_at="2026-09-01T10:00")
    db.mark(conn, "a", 1, "sent", sent_at=now)
    db.mark(conn, "b", 1, "failed")
    assert db.unresolved_rows(conn, now, timedelta(minutes=5)) == []


def test_counts_are_the_on_camera_surface(conn):
    assert db.counts(conn) == {"reminder_log": 0, "stops": 0}
    insert_claimed(conn)
    db.record_stop(conn, "2", "reply", "<r@x>", now=at("2026-09-01T00:00"))
    assert db.counts(conn) == {"reminder_log": 1, "stops": 1}
