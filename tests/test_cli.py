"""Spec section 4.1. A stranger clones this repo and types a command; nothing they can
reasonably type should answer with a traceback."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from chase import cli
from chase.cli import main
from chase.report import RunReport
from chase.run import Deps


def _raise(exc):
    def raiser(*args, **kwargs):
        raise exc

    return raiser


class _NoHubSpot:
    """Answers the protocol, reaches nothing."""

    def search_candidates(self, client):
        return []

    def read_deal(self, deal_id):  # pragma: no cover - no candidate ever gets this far
        raise AssertionError("no candidates, so no deal is read")

    def patch_after_send(self, deal_id, last_chase_at, stage):  # pragma: no cover
        raise AssertionError("nothing is sent")

    def patch_stage(self, deal_id, stage):  # pragma: no cover
        raise AssertionError("nothing is stopped")


class _NoMail:
    def poll_replies(self):
        return []

    def send(self, message):  # pragma: no cover - overridden where a send is expected
        raise AssertionError("nothing is sent")


def _offline_deps() -> Deps:
    return Deps(
        hubspot=_NoHubSpot(),
        mail=_NoMail(),
        build_message=lambda *a, **k: {"to": "x"},
        opener=lambda c, cand, step: ("Hope the week is treating you well.", False),
        make_message_id=lambda c: "<m@test>",
    )




def test_status_on_an_empty_database(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "d" / "chase.db"), "status"]) == 0
    out = capsys.readouterr().out
    assert "reminder_log: 0 rows, stops: 0 rows" in out
    assert "clock:" in out and "client: harbourline" in out


def test_status_prints_the_row_count_first(tmp_path, capsys):
    """The count line is the on-camera surface for "the re-run added zero rows", so it
    is the first line and not buried under a table. Spec section 13 beat 3."""
    main(["--db", str(tmp_path / "chase.db"), "status"])
    assert capsys.readouterr().out.splitlines()[0].startswith("reminder_log:")


def test_reset_clears_the_send_log(tmp_path, capsys):
    dbpath = str(tmp_path / "chase.db")
    main(["--db", dbpath, "status"])
    from chase import db

    conn = db.connect(dbpath)
    conn.execute(
        "INSERT INTO reminder_log (deal_id, step, status, claimed_at, message_id) "
        "VALUES ('1', 1, 'sent', '2026-09-01T00:00:00+00:00', '<m@x>')"
    )
    conn.close()
    assert main(["--db", dbpath, "reset"]) == 0
    main(["--db", dbpath, "status"])
    assert "reminder_log: 0 rows" in capsys.readouterr().out


def test_offset_flag_moves_the_clock(tmp_path, capsys):
    main(["--db", str(tmp_path / "chase.db"), "--offset", "4d13h", "status"])
    assert "offset: 4d13h" in capsys.readouterr().out


def test_a_bad_offset_is_a_message_not_a_traceback(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "chase.db"), "--offset", "4x", "status"]) == 2
    assert "bad --offset" in capsys.readouterr().err


def test_an_unknown_client_is_a_message_not_a_traceback(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "chase.db"), "--client", "nobody", "status"]) == 2
    assert "config error" in capsys.readouterr().err


# `run` reaches the network, so every test here either stops before it does or replaces
# the collaborators. Nothing in this file may open a socket: a suite that quietly talks to
# a live portal is a suite nobody can run on a plane, and it would write to real deals.


@pytest.fixture
def no_dotenv(monkeypatch, tmp_path):
    """Point the loader at a file that is not there and clear what it would have set."""
    monkeypatch.setattr(cli, "DEFAULT_ENV", tmp_path / "absent.env")
    for key in ("HUBSPOT_TOKEN", "CHASE_SMTP_HOST", "CHASE_SMTP_PORT", "MAILPIT_URL"):
        monkeypatch.delenv(key, raising=False)


def test_run_without_a_token_is_a_message_not_a_traceback(tmp_path, capsys, no_dotenv):
    assert main(["--db", str(tmp_path / "chase.db"), "run"]) == 2
    err = capsys.readouterr().err
    assert "config error" in err and "HUBSPOT_TOKEN" in err


def test_run_prints_the_summary_line_and_exits_zero(tmp_path, capsys, no_dotenv, monkeypatch):
    monkeypatch.setattr(cli, "build_deps", lambda client: _offline_deps())
    assert main(["--db", str(tmp_path / "chase.db"), "run"]) == 0
    assert "status ok" in capsys.readouterr().out


def test_a_failed_report_makes_the_exit_code_non_zero(tmp_path, capsys, no_dotenv, monkeypatch):
    """A scheduler reads the exit code, not the summary line. Driving a real rung to
    failure here would mean faking the clock and the send window too, so this asserts
    the one thing cmd_run decides: what a report's status does to the exit code."""
    canned = RunReport(
        client="harbourline", now=datetime(2026, 9, 1, tzinfo=timezone.utc), clock_offset="0d"
    )
    canned.failed = 1
    canned.error = "HubSpot read failed for 2: 429"
    monkeypatch.setattr(cli, "build_deps", lambda client: _offline_deps())
    monkeypatch.setattr(cli, "run_ladder", lambda *a, **k: canned)
    assert main(["--db", str(tmp_path / "chase.db"), "run"]) == 1
    captured = capsys.readouterr()
    assert "status failed" in captured.out
    assert "429" in captured.err, "the error travels where a human will see it"


def test_run_aborted_says_nothing_was_claimed(tmp_path, capsys, no_dotenv, monkeypatch):
    deps = _offline_deps()
    deps.hubspot.search_candidates = _raise(RuntimeError("429 from HubSpot"))
    monkeypatch.setattr(cli, "build_deps", lambda client: deps)
    assert main(["--db", str(tmp_path / "chase.db"), "run"]) == 1
    assert "nothing was claimed" in capsys.readouterr().err


def test_dry_is_accepted_and_claims_nothing(tmp_path, capsys, no_dotenv, monkeypatch):
    from chase import db

    monkeypatch.setattr(cli, "build_deps", lambda client: _offline_deps())
    dbpath = str(tmp_path / "chase.db")
    assert main(["--db", dbpath, "run", "--dry"]) == 0
    conn = db.connect(dbpath)
    assert db.counts(conn)["reminder_log"] == 0
    conn.close()


# --- the .env loader ---------------------------------------------------------


def test_dotenv_fills_only_what_is_missing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("HUBSPOT_TOKEN=from-file\nMAILPIT_URL=http://from-file:8025\n")
    monkeypatch.setenv("HUBSPOT_TOKEN", "already-exported")
    monkeypatch.delenv("MAILPIT_URL", raising=False)
    cli.load_dotenv(env)
    assert os.environ["HUBSPOT_TOKEN"] == "already-exported", "an export wins over the file"
    assert os.environ["MAILPIT_URL"] == "http://from-file:8025"


def test_dotenv_ignores_comments_blanks_and_quotes(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# a comment\n\nCHASE_SMTP_PORT="2525"\nJUST_A_NAME=\nnot a pair\n')
    monkeypatch.delenv("CHASE_SMTP_PORT", raising=False)
    monkeypatch.delenv("JUST_A_NAME", raising=False)
    cli.load_dotenv(env)
    assert os.environ["CHASE_SMTP_PORT"] == "2525"
    assert "JUST_A_NAME" not in os.environ, "a name with no value sets nothing"


def test_a_missing_dotenv_is_silence(tmp_path):
    cli.load_dotenv(tmp_path / "absent.env")  # must not raise


def test_unresolved_rows_are_reported_with_the_manual_remedy(tmp_path, capsys, monkeypatch):
    from chase import db

    dbpath = str(tmp_path / "chase.db")
    conn = db.connect(dbpath)
    db.init(conn)
    conn.execute(
        "INSERT INTO reminder_log (deal_id, step, status, claimed_at, message_id) "
        "VALUES ('1', 1, 'claimed', '2020-01-01T00:00:00+00:00', '<stuck@x>')"
    )
    conn.close()
    main(["--db", dbpath, "status"])
    out = capsys.readouterr().out
    assert "UNRESOLVED: 1" in out
    assert "Nothing resends them automatically" in out
