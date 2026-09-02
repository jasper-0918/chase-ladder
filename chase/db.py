"""The send log. Spec sections 4.4 and 4.5.

Two tables and no more. `reminder_log` answers "has this rung been sent", `stops`
answers "should this deal be chased at all". HubSpot holds the quotes themselves, so
there is no mirror here and nothing to keep in sync.

Concurrency, which is the part worth reading:

    Two writers share this file, the ladder run and whatever records a stop (the reply
    hook, the /stop route, a stage read). SQLite in WAL mode allows exactly one writer
    at a time, so the two serialise through the file lock rather than through anything
    clever in Python.

    Every transaction here opens with an explicit BEGIN IMMEDIATE, which takes the write
    lock up front. Python's sqlite3 defaults to DEFERRED, where a transaction that reads
    and then writes can fail with SQLITE_BUSY_SNAPSHOT after another connection commits
    in between, and the busy timeout does not rescue that case. Hence
    `autocommit=True` on connect plus hand-written BEGIN IMMEDIATE, verified against
    sqlite.org/isolation.html and rescode.html.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

from chase import clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminder_log (
    deal_id     TEXT    NOT NULL,
    step        INTEGER NOT NULL,
    status      TEXT    NOT NULL CHECK (status IN ('claimed', 'sent', 'failed')),
    claimed_at  TEXT    NOT NULL,
    sent_at     TEXT,
    message_id  TEXT    NOT NULL,
    UNIQUE (deal_id, step)
);

CREATE TABLE IF NOT EXISTS stops (
    deal_id     TEXT    NOT NULL,
    reason      TEXT    NOT NULL CHECK (reason IN ('reply', 'stage', 'manual')),
    ref         TEXT    NOT NULL,
    stopped_at  TEXT    NOT NULL,
    UNIQUE (deal_id, ref)
);
"""


class Claim(Enum):
    """What happened when the ladder tried to take a rung."""

    CLAIMED = "claimed"           # the row is ours, the send may proceed
    STOPPED = "stopped"           # a stop row exists, nothing was written
    ALREADY_LOGGED = "already"    # a claimed or sent row already holds this step
    SENT = "sent"                 # the send succeeded and the row says so
    FAILED = "failed"             # the server refused it outright, retryable next run
    UNKNOWN = "unknown"           # ambiguous outcome, the row stays claimed, never retried


def connect(path: Path | str) -> sqlite3.Connection:
    """One connection per request or per CLI invocation, never a shared global."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def mark(
    conn: sqlite3.Connection,
    deal_id: str,
    step: int,
    status: str,
    sent_at: datetime | None = None,
) -> None:
    """Move a claimed row to `sent` or `failed`. Never called for an ambiguous send:
    that row stays `claimed` on purpose, see spec section 4.4."""
    if status not in ("sent", "failed"):
        raise ValueError(f"mark() takes 'sent' or 'failed', not {status!r}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE reminder_log SET status = ?, sent_at = ? WHERE deal_id = ? AND step = ?",
            (status, sent_at.isoformat() if sent_at else None, deal_id, step),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def record_stop(
    conn: sqlite3.Connection,
    deal_id: str,
    reason: str,
    ref: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Record that a deal must not be chased again. Returns True when a row was added.

    Every stop source funnels through here: a reply seen by the poll or the webhook, a
    stage read that found Won or Lost, a human calling /stop. `UNIQUE (deal_id, ref)` is
    what makes a reply seen twice, once by the webhook and once by the poll, one row.

    A stop is final. Nothing in this system deletes one, so moving a deal back in
    HubSpot does not resume a ladder that a reply already stopped. Spec section 5.
    """
    if reason not in ("reply", "stage", "manual"):
        raise ValueError(f"unknown stop reason {reason!r}")
    stopped_at = (now or clock.now()).isoformat()
    ref = ref or f"manual:{stopped_at}"
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO stops (deal_id, reason, ref, stopped_at) VALUES (?, ?, ?, ?)",
            (deal_id, reason, ref, stopped_at),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return cur.rowcount == 1


def is_stopped(conn: sqlite3.Connection, deal_id: str) -> bool:
    return conn.execute("SELECT 1 FROM stops WHERE deal_id = ?", (deal_id,)).fetchone() is not None


def stopped_deal_ids(conn: sqlite3.Connection) -> set[str]:
    return {row["deal_id"] for row in conn.execute("SELECT DISTINCT deal_id FROM stops")}


def log_rows_for(conn: sqlite3.Connection, deal_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM reminder_log WHERE deal_id = ? ORDER BY step", (deal_id,)
        )
    )


def all_log_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM reminder_log ORDER BY deal_id, step"))


def unresolved_rows(
    conn: sqlite3.Connection, now: datetime, unresolved_after: timedelta
) -> list[sqlite3.Row]:
    """Rows still `claimed` and older than the grace window.

    The threshold is a grace window rather than the run's start time. A row claimed one
    second before this run began, still inside its SMTP call, is healthy; counting it
    would page the owner about a run that is going fine. Spec section 4.5 step 10.
    """
    cutoff = (now - unresolved_after).isoformat()
    return list(
        conn.execute(
            "SELECT * FROM reminder_log WHERE status = 'claimed' AND claimed_at < ?", (cutoff,)
        )
    )


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row counts for `chase status`, the on-camera "zero new rows" surface."""
    return {
        "reminder_log": conn.execute("SELECT COUNT(*) FROM reminder_log").fetchone()[0],
        "stops": conn.execute("SELECT COUNT(*) FROM stops").fetchone()[0],
    }


# ---------------------------------------------------------------------------
# The claim transaction. Spec sections 4.5 step 8 and 12.
# ---------------------------------------------------------------------------
def claim(
    conn: sqlite3.Connection,
    deal_id: str,
    step: int,
    message_id: str,
    now: datetime | None = None,
) -> Claim:
    """Take a rung, or explain why not. The tests in tests/test_claim.py are the spec.

    This is the transaction the whole design hangs on, and the reasoning matters more
    than the seven lines of code:

      1. `conn.execute("BEGIN IMMEDIATE")` FIRST, before reading anything. That takes
         the write lock up front, so the read below and the insert after it cannot be
         separated by another connection's commit. With Python's default DEFERRED
         transaction this same code raises SQLITE_BUSY_SNAPSHOT under concurrency, and
         `busy_timeout` does not save it.

      2. Read `stops` for this deal. If a stop exists, ROLLBACK and return
         `Claim.STOPPED`. This is the check that makes "the chasing stops when they
         reply" true even when the reply lands mid-run.

      3. The insert's `ON CONFLICT (deal_id, step) DO UPDATE ... WHERE
         reminder_log.status = 'failed'` is the whole design: a rung the mail server
         refused is retryable, a rung that is `claimed` or `sent` is not. rowcount 1
         means the row is ours; 0 means a claimed or sent row already holds this step,
         so ROLLBACK and return `Claim.ALREADY_LOGGED`.

      4. COMMIT, return `Claim.CLAIMED`.

      5. Any exception: ROLLBACK and re-raise. Never leave a transaction open.

    `claimed_at` is `now or clock.now()`, the wall clock at THIS moment, not the run's
    fixed `now`. Stamping it with the run's start would make a healthy overlapping run
    look unresolved and alert the owner for nothing.
    """
    claimed_at = (now or clock.now()).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM stops WHERE deal_id = ?", (deal_id,)).fetchone():
            conn.execute("ROLLBACK")
            return Claim.STOPPED
        cur = conn.execute(
            "INSERT INTO reminder_log (deal_id, step, status, claimed_at, sent_at, message_id) "
            "VALUES (?, ?, 'claimed', ?, NULL, ?) "
            "ON CONFLICT (deal_id, step) DO UPDATE SET "
            "    status = 'claimed', claimed_at = excluded.claimed_at, "
            "    message_id = excluded.message_id, sent_at = NULL "
            "WHERE reminder_log.status = 'failed'",
            (deal_id, step, claimed_at, message_id),
        )
        if cur.rowcount == 0:
            conn.execute("ROLLBACK")
            return Claim.ALREADY_LOGGED
        conn.execute("COMMIT")
        return Claim.CLAIMED
    except Exception:
        conn.execute("ROLLBACK")
        raise
