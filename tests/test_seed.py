"""The demo board. Spec section 4.12.

The board is fixed on purpose, so these tests assert the shape a demo depends on: that
all three rungs fire on the first run, that every deal can be found again by `reset`, and
that a quote date is midnight UTC because HubSpot refuses anything else.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from chase.config import load_client
from chase.ladder import Candidate, due_step
from chase.seed import (
    CONTACT_DOMAIN,
    DEAL_PREFIX,
    LIVE_AGES,
    archive_seed,
    demo_deals,
    is_seeded_contact,
    is_seeded_deal,
    seed,
)

NOW = datetime(2026, 9, 1, 13, 45, 30, tzinfo=timezone.utc)  # deliberately not midnight


@pytest.fixture
def client(config_dir):
    return load_client("harbourline", config_dir)


class FakeHubSpot:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.contacts: list[tuple] = []
        self.deals: list[dict] = []
        self.associations: list[tuple] = []
        self._next = 0

    def create_contact(self, email, first_name, last_name):
        self.contacts.append((email, first_name, last_name))
        self._next += 1
        return f"c{self._next}"

    def create_deal(self, name, amount, stage, quote_sent_at=None):
        if self.fail_on and self.fail_on in name:
            raise RuntimeError("400 from HubSpot")
        self.deals.append(
            {"name": name, "amount": amount, "stage": stage, "quote_sent_at": quote_sent_at}
        )
        self._next += 1
        return f"d{self._next}"

    def associate_contact(self, deal_id, contact_id):
        self.associations.append((deal_id, contact_id))


# --- the board ---------------------------------------------------------------


def test_the_board_is_twenty_four_deals(client):
    assert len(demo_deals(client, NOW)) == 24


def test_the_board_is_identical_every_time(client):
    """A fixed set, so a recording can be retaken without the numbers moving."""
    first = demo_deals(client, NOW)
    second = demo_deals(client, NOW)
    assert [d.name for d in first] == [d.name for d in second]
    assert [d.amount for d in first] == [d.amount for d in second]


def test_twenty_live_quotes_and_four_pieces_of_dressing(client):
    stages = [d.stage for d in demo_deals(client, NOW)]
    assert stages.count("Sent") == 20
    assert stages.count("Draft") == 1
    assert stages.count("Won") == 2
    assert stages.count("Lost") == 1


def test_every_deal_is_findable_by_reset(client):
    """`reset` archives what the seed wrote and nothing else, so both marks must hold."""
    for deal in demo_deals(client, NOW):
        assert deal.name.startswith(DEAL_PREFIX)
        assert deal.contact_email.endswith(f"@{CONTACT_DOMAIN}")


def test_no_two_deals_share_a_contact(client):
    """The spec's reason: a seed that writes twice gives a stranger two answers to
    "where is this customer's email"."""
    emails = [d.contact_email for d in demo_deals(client, NOW)]
    assert len(set(emails)) == len(emails)


def test_every_work_category_comes_from_the_client_yaml(client):
    """Only these values may reach the model, spec 4.9, so the seed may not invent one."""
    for deal in demo_deals(client, NOW):
        assert any(category in deal.name for category in client.work_categories)


# --- the dates ---------------------------------------------------------------


def test_a_quote_date_is_midnight_even_when_the_clock_is_not(client):
    """HubSpot refuses a Date property that is not midnight UTC: 04:30 came back
    `400 INVALID_DATE`, "is at 4:30:0.0 UTC, not midnight!"."""
    for deal in demo_deals(client, NOW):
        stamp = deal.quote_sent_at
        assert (stamp.hour, stamp.minute, stamp.second, stamp.microsecond) == (0, 0, 0, 0)


def test_the_time_of_day_is_dropped_not_rounded(client):
    """Rounding 13:45 up would move the quote a day later and delay its whole ladder."""
    oldest = max(LIVE_AGES)
    dates = {d.quote_sent_at.date() for d in demo_deals(client, NOW)}
    assert (NOW - timedelta(days=oldest)).date() in dates
    assert (NOW - timedelta(days=1)).date() in dates


def test_the_board_moves_with_the_clock_not_the_wall(client):
    """Seeded under one offset and run under another behaves like real days passing."""
    later = demo_deals(client, NOW + timedelta(days=4))
    assert later[0].quote_sent_at == demo_deals(client, NOW)[0].quote_sent_at + timedelta(days=4)


def test_all_three_rungs_are_due_on_the_first_run(client):
    """A board that only ever shows step 1 does not show a ladder."""
    due = []
    for deal in demo_deals(client, NOW):
        if deal.stage != "Sent":
            continue
        candidate = Candidate(
            deal_id=deal.name,
            name=deal.name,
            amount=deal.amount,
            stage=deal.stage,
            quote_sent_at=deal.quote_sent_at,
            contact_email=deal.contact_email,
        )
        step = due_step(candidate, client.ladder, NOW, [])
        if step is not None:
            due.append(step.step)
    assert set(due) == {1, 2, 3}, f"expected every rung, got {sorted(set(due))}"


# --- writing it ---------------------------------------------------------------


# --- taking it back ----------------------------------------------------------
# reset runs against a portal that may hold real deals. Every test below exists to
# prove one thing: nothing without the seed's mark is ever archived.


class FakePortal:
    def __init__(self, deals, contacts, fail_on=None):
        self._deals = deals
        self._contacts = contacts
        self.fail_on = fail_on
        self.archived_deals: list[str] = []
        self.archived_contacts: list[str] = []

    def list_deals(self, limit=200):
        return list(self._deals)

    def list_contacts(self, limit=200):
        return list(self._contacts)

    def archive_deal(self, deal_id):
        if self.fail_on == deal_id:
            raise RuntimeError("409 from HubSpot")
        self.archived_deals.append(deal_id)

    def archive_contact(self, contact_id):
        if self.fail_on == contact_id:
            raise RuntimeError("409 from HubSpot")
        self.archived_contacts.append(contact_id)


@pytest.mark.parametrize(
    "name,seeded",
    [
        ("Q-0400 website rebuild, Marrickville Cycles", True),
        ("Q-0999 anything", True),
        ("Roof replacement, real customer", False),
        ("q-0400 lowercase prefix", False),
        ("A quote Q-0400 mentioned mid-name", False),
        ("", False),
    ],
)
def test_only_the_seeds_own_prefix_counts(name, seeded):
    assert is_seeded_deal(name) is seeded


@pytest.mark.parametrize(
    "email,seeded",
    [
        ("nadia.fenton@customer.example", True),
        ("NADIA.FENTON@CUSTOMER.EXAMPLE", True),
        ("someone@realbusiness.com.au", False),
        ("customer.example@gmail.com", False),
        ("", False),
    ],
)
def test_only_the_seeds_own_domain_counts(email, seeded):
    assert is_seeded_contact(email) is seeded


def test_archive_leaves_real_deals_and_real_people_alone():
    """The test this whole module exists for."""
    portal = FakePortal(
        deals=[
            {"id": "1", "name": "Q-0400 website rebuild, Marrickville Cycles"},
            {"id": "2", "name": "Kitchen fitout, a paying customer"},
            {"id": "3", "name": "Q-0401 SEO retainer, Brunswick Bakehouse"},
        ],
        contacts=[
            {"id": "c1", "email": "nadia.fenton@customer.example"},
            {"id": "c2", "email": "real.person@theirbusiness.com.au"},
        ],
    )
    report = archive_seed(portal)
    assert portal.archived_deals == ["1", "3"]
    assert portal.archived_contacts == ["c1"]
    assert report.deals == 2 and report.contacts == 1
    assert report.kept == 2, "the real deal and the real person"


def test_an_empty_portal_archives_nothing():
    report = archive_seed(FakePortal(deals=[], contacts=[]))
    assert (report.deals, report.contacts, report.failed) == (0, 0, 0)


def test_one_archive_that_fails_does_not_stop_the_rest():
    portal = FakePortal(
        deals=[
            {"id": "1", "name": "Q-0400 a"},
            {"id": "2", "name": "Q-0401 b"},
            {"id": "3", "name": "Q-0402 c"},
        ],
        contacts=[],
        fail_on="2",
    )
    report = archive_seed(portal)
    assert portal.archived_deals == ["1", "3"]
    assert report.deals == 2 and report.failed == 1
    assert "deal 2" in report.errors[0]


def test_deals_go_before_contacts():
    """A failure partway should leave orphaned contacts, not deals with nobody to
    email: the ladder cannot see the first and reports the second as broken."""
    order: list[str] = []

    class Ordered(FakePortal):
        def archive_deal(self, deal_id):
            order.append("deal")

        def archive_contact(self, contact_id):
            order.append("contact")

    archive_seed(
        Ordered(
            deals=[{"id": "1", "name": "Q-0400 a"}],
            contacts=[{"id": "c1", "email": "a@customer.example"}],
        )
    )
    assert order == ["deal", "contact"]


def test_a_seeded_board_is_fully_recoverable(client):
    """Seed then archive leaves the portal as it was found."""
    written = FakeHubSpot()
    seed(written, client, NOW)
    portal = FakePortal(
        deals=[{"id": str(i), "name": d["name"]} for i, d in enumerate(written.deals)],
        contacts=[{"id": f"c{i}", "email": c[0]} for i, c in enumerate(written.contacts)],
    )
    report = archive_seed(portal)
    assert report.deals == 24 and report.contacts == 24
    assert report.kept == 0


def test_seed_writes_a_contact_a_deal_and_an_association_for_each(client):
    hubspot = FakeHubSpot()
    report = seed(hubspot, client, NOW)
    assert report.created == 24
    assert len(hubspot.contacts) == 24
    assert len(hubspot.deals) == 24
    assert len(hubspot.associations) == 24


def test_one_deal_that_will_not_write_does_not_stop_the_rest(client):
    """A half-seeded portal is visible and resettable. A raise halfway is neither."""
    hubspot = FakeHubSpot(fail_on="Marrickville Cycles")
    report = seed(hubspot, client, NOW)
    assert report.created == 23
    assert report.failed == 1
    assert "Marrickville Cycles" in report.errors[0]
    assert "seed: created 23, failed 1" == report.summary_line()
