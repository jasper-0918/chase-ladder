"""`python -m chase <command>`. Spec section 4.1.

Thin on purpose: the CLI parses arguments, wires the real collaborators, and prints. Every
decision lives in `run.py` and the modules under it, which is why the offline tests need
no CLI at all.

Commands that reach HubSpot, Mailpit or Groq arrive in sitting 2. Today `status` works
against the local database, and `run` refuses clearly rather than half-running.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

from chase import clock, db
from chase.config import ConfigError, load_client

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "data" / "chase.db"


def _client_name(args) -> str:
    return args.client or os.environ.get("CHASE_CLIENT", "harbourline")


def _unresolved_after() -> timedelta:
    return clock.parse_offset(os.environ.get("CHASE_UNRESOLVED_AFTER", "5m"))


def cmd_status(args) -> int:
    """Row counts first, then the table. The count line is the on-camera surface for
    "the second run added zero rows"."""
    client = load_client(_client_name(args), REPO_ROOT / "config")
    conn = db.connect(args.db)
    db.init(conn)
    try:
        counts = db.counts(conn)
        print(f"reminder_log: {counts['reminder_log']} rows, stops: {counts['stops']} rows")
        rows = db.all_log_rows(conn)
        if rows:
            print(f"{'deal':<12} {'step':>4} {'status':<8} {'claimed_at':<26} sent_at")
            for row in rows:
                print(
                    f"{row['deal_id']:<12} {row['step']:>4} {row['status']:<8} "
                    f"{row['claimed_at']:<26} {row['sent_at'] or ''}"
                )
        stops = list(conn.execute("SELECT * FROM stops ORDER BY stopped_at"))
        if stops:
            print()
            print(f"{'deal':<12} {'reason':<8} ref")
            for row in stops:
                print(f"{row['deal_id']:<12} {row['reason']:<8} {row['ref']}")
        unresolved = db.unresolved_rows(conn, clock.now(), _unresolved_after())
        if unresolved:
            print()
            print(f"UNRESOLVED: {len(unresolved)} claimed rows older than the grace window.")
            print("Settle each by hand from Mailpit, by its message_id, then set the row to")
            print("sent or failed. Nothing resends them automatically, on purpose.")
        print()
        print(f"clock: {clock.now().isoformat()}  offset: {os.environ.get(clock.ENV_VAR, '0d')}")
        print(f"client: {client.client} ({client.business_name}), window {client.send_window.tz}")
        return 0
    finally:
        conn.close()


def cmd_run(args) -> int:
    load_client(_client_name(args), REPO_ROOT / "config")
    print(
        "run needs the HubSpot, Mailpit and Groq clients, which arrive in sitting 2.\n"
        "The decision path they plug into is finished and tested: see tests/test_run.py.",
        file=sys.stderr,
    )
    return 2


def cmd_reset(args) -> int:
    """The SQLite half. Clearing Mailpit and re-seeding HubSpot arrive in sitting 2."""
    conn = db.connect(args.db)
    db.init(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM reminder_log")
        conn.execute("DELETE FROM stops")
        conn.execute("COMMIT")
        print(f"cleared the send log at {args.db}")
        print("Mailpit messages and the HubSpot seed are not touched yet (sitting 2).")
        return 0
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m chase", description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"default {DEFAULT_DB}")
    parser.add_argument("--client", help="client name, default CHASE_CLIENT or harbourline")
    parser.add_argument("--offset", help="clock offset for this invocation, e.g. 4d13h")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler, help_text in [
        ("status", cmd_status, "print the send log, the stops and the clock"),
        ("run", cmd_run, "one ladder pass"),
        ("reset", cmd_reset, "clear the local send log"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(handler=handler)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.offset:
        try:
            clock.parse_offset(args.offset)      # fail fast on a typo, never silently 0d
        except ValueError as exc:
            print(f"bad --offset: {exc}", file=sys.stderr)
            return 2
        os.environ[clock.ENV_VAR] = args.offset
    try:
        return args.handler(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except (sqlite3.OperationalError, OSError) as exc:
        print(f"cannot open the send log at {args.db}: {exc}", file=sys.stderr)
        print("Pass --db with a writable path, or run from the repository root.", file=sys.stderr)
        return 2
