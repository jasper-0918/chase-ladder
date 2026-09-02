"""Spec section 4.5 step 4."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from chase.config import load_client
from chase.ladder import Candidate, due_step, highest_blocking_step, is_ladder_exhausted

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def row(step: int, status: str) -> dict:
    return {"step": step, "status": status}


def quote(days_old: float) -> Candidate:
    return Candidate(
        deal_id="d1",
        name=f"Q-0412 website rebuild, {days_old}d",
        amount=4200.0,
        stage="Sent",
        quote_sent_at=NOW - timedelta(days=days_old),
    )


@pytest.mark.parametrize(
    "client_name, ages_and_expected",
    [
        ("harbourline", [(0, None), (2.9, None), (3, 1), (6.9, 1), (7, 2), (13.9, 2), (14, 3), (40, 3)]),
        ("lakeshore", [(1.9, None), (2, 1), (4.9, 1), (5, 2), (9.9, 2), (10, 3), (19.9, 3), (20, 4)]),
    ],
)
def test_due_steps_for_a_fresh_quote(client_name, ages_and_expected, config_dir):
    """Parametrised over both clients, which is what proves the ladder is data."""
    client = load_client(client_name, config_dir)
    for age, expected in ages_and_expected:
        step = due_step(quote(age), client.ladder, NOW, [])
        assert (step.step if step else None) == expected, f"{client_name} at {age}d"


def test_highest_due_step_only(config_dir):
    """A ten day old quote on its first run gets step 2, not steps 1 and 2 in a minute."""
    client = load_client("harbourline", config_dir)
    assert due_step(quote(10), client.ladder, NOW, []).step == 2


def test_a_skipped_step_is_skipped_for_good(config_dir):
    """Having sent step 2, the ladder never goes back for the step 1 it skipped."""
    client = load_client("harbourline", config_dir)
    log = [row(2, "sent")]
    assert due_step(quote(10), client.ladder, NOW, log) is None
    assert due_step(quote(14), client.ladder, NOW, log).step == 3


def test_a_sent_step_blocks_that_step(config_dir):
    client = load_client("harbourline", config_dir)
    assert due_step(quote(3), client.ladder, NOW, [row(1, "sent")]) is None


def test_a_claimed_step_blocks_permanently(config_dir):
    """A claimed row's outcome is unknown, so resending could deliver twice. It blocks
    until a human resolves it. Spec section 4.4."""
    client = load_client("harbourline", config_dir)
    assert due_step(quote(3), client.ladder, NOW, [row(1, "claimed")]) is None
    assert due_step(quote(30), client.ladder, NOW, [row(1, "claimed")]).step == 3


def test_a_failed_step_is_retryable(config_dir):
    """The server refused that rung outright, so it may be tried again. This is the
    difference between `failed` and `claimed`, and it is the whole reason the two states
    are separate. Spec section 4.4."""
    client = load_client("harbourline", config_dir)
    assert due_step(quote(3), client.ladder, NOW, [row(1, "failed")]).step == 1


def test_a_failed_step_does_not_hold_the_ladder_back(config_dir):
    """If a later rung has come due in the meantime, the ladder moves on rather than
    retrying an old rung a fortnight late."""
    client = load_client("harbourline", config_dir)
    assert due_step(quote(8), client.ladder, NOW, [row(1, "failed")]).step == 2


@pytest.mark.parametrize(
    "rows, expected",
    [([], 0), ([row(1, "sent")], 1), ([row(1, "failed")], 0), ([row(1, "sent"), row(2, "claimed")], 2),
     ([row(1, "sent"), row(2, "failed")], 1), ([row(3, "claimed")], 3)],
)
def test_highest_blocking_step(rows, expected):
    assert highest_blocking_step(rows) == expected


def test_ladder_exhausted_counted(config_dir):
    """Only the last rung exhausts a ladder, and only when it actually went out."""
    client = load_client("harbourline", config_dir)
    assert is_ladder_exhausted(client, [row(3, "sent")]) is True
    assert is_ladder_exhausted(client, [row(3, "claimed")]) is True
    assert is_ladder_exhausted(client, [row(1, "sent"), row(2, "sent")]) is False
    assert is_ladder_exhausted(client, [row(3, "failed")]) is False, "a refused last rung retries"


def test_exhausted_differs_between_clients(config_dir):
    """Lakeshore has a fourth rung, so a step 3 send does not exhaust its ladder."""
    h = load_client("harbourline", config_dir)
    l = load_client("lakeshore", config_dir)
    assert is_ladder_exhausted(h, [row(3, "sent")]) is True
    assert is_ladder_exhausted(l, [row(3, "sent")]) is False
    assert is_ladder_exhausted(l, [row(4, "sent")]) is True


def test_boundary_is_inclusive_at_the_exact_instant(config_dir):
    """A quote that turns three days old exactly is due, not due tomorrow."""
    client = load_client("harbourline", config_dir)
    exact = Candidate("d1", "Q", 1.0, "Sent", NOW - timedelta(days=3))
    assert due_step(exact, client.ladder, NOW, []).step == 1
    one_second_early = Candidate("d1", "Q", 1.0, "Sent", NOW - timedelta(days=3) + timedelta(seconds=1))
    assert due_step(one_second_early, client.ladder, NOW, []) is None
