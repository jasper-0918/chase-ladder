"""`python -m chase <command>`. Spec section 4.1.

Thin on purpose: the CLI parses arguments, wires the real collaborators, and prints. Every
decision lives in `run.py` and the modules under it, which is why the offline tests need
no CLI at all.

`run` builds the real collaborators and hands them to `run_ladder`: the HubSpot client,
an SMTP sender and the Mailpit reply poll. It reads its secrets from the environment, and
from a local `.env` if one is there, so nothing in the repository can authenticate to
anything.
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
from chase.hubspot import HubSpotClient
from chase.mailer import SmtpSender, make_message_id
from chase.mailpit import DEFAULT_BASE_URL, Mail, MailpitPoller
from chase.opener import template_opener
from chase.run import Deps, RunAborted, run_ladder
from chase.templates import build_message

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "data" / "chase.db"
DEFAULT_ENV = REPO_ROOT / ".env"


def _client_name(args) -> str:
    return args.client or os.environ.get("CHASE_CLIENT", "harbourline")


def _unresolved_after() -> timedelta:
    return clock.parse_offset(os.environ.get("CHASE_UNRESOLVED_AFTER", "5m"))


def load_dotenv(path: Path = DEFAULT_ENV) -> None:
    """Read `KEY=value` lines out of `.env` into the environment.

    Fifteen lines instead of a dependency, and it never overwrites a variable that is
    already set, so an explicit `HUBSPOT_TOKEN=... python -m chase run` still wins. A
    missing file is silence: the variables may well be exported already.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in, or export it."
        )
    return value


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


def build_deps(client) -> Deps:
    """The real collaborators. Constructing them opens no connection, so a bad token is
    reported by the first call that needs it rather than by import time."""
    return Deps(
        hubspot=HubSpotClient(token=_require_env("HUBSPOT_TOKEN")),
        mail=Mail(
            sender=SmtpSender(
                host=os.environ.get("CHASE_SMTP_HOST", "localhost"),
                port=int(os.environ.get("CHASE_SMTP_PORT") or 1025),
            ),
            poller=MailpitPoller(base_url=os.environ.get("MAILPIT_URL") or DEFAULT_BASE_URL),
        ),
        build_message=lambda *a: build_message(*a, repo_root=REPO_ROOT),
        opener=template_opener,
        make_message_id=make_message_id,
        unresolved_after=_unresolved_after(),
    )


def cmd_run(args) -> int:
    """One ladder pass. Exit 0 only when the report says `ok`, so a scheduler that reads
    the exit code sees a failed rung as a failure without parsing the summary."""
    load_dotenv(DEFAULT_ENV)
    client = load_client(_client_name(args), REPO_ROOT / "config")
    deps = build_deps(client)
    conn = db.connect(args.db)
    db.init(conn)
    try:
        report = run_ladder(
            conn,
            client,
            clock.now(),
            deps,
            clock_offset=os.environ.get(clock.ENV_VAR, "0d"),
            dry=getattr(args, "dry", False),
        )
    except RunAborted as exc:
        # Nothing was claimed, so this is safe to retry once whatever broke is back.
        print(f"run aborted, nothing was claimed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(report.summary_line())
    if report.error:
        print(f"error: {report.error}", file=sys.stderr)
    return 0 if report.status == "ok" else 1


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
        if name == "run":
            p.add_argument(
                "--dry",
                action="store_true",
                help="read, decide and report, but claim and send nothing",
            )
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
