"""Spec section 4.1. A stranger clones this repo and types a command; nothing they can
reasonably type should answer with a traceback."""

from __future__ import annotations

import pytest

from chase.cli import main


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


def test_run_refuses_clearly_until_sitting_two(tmp_path, capsys):
    """Better an explicit refusal than a half-run against collaborators that do not
    exist yet."""
    assert main(["--db", str(tmp_path / "chase.db"), "run"]) == 2
    assert "sitting 2" in capsys.readouterr().err


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
