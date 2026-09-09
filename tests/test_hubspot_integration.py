"""The real HubSpot client driving a real run. Spec section 4.5, end to end.

tests/test_run.py isolates the orchestration behind a hand-written FakeHubSpot, which is
the right shape for testing decisions but proves nothing about the client that will
actually talk to the portal. This file closes that gap: `HubSpotClient` over a fake
transport, the real `db.claim`, the real send-window calculation and the real message
builder, with only the mail transport and the model call stubbed.

What it is really guarding: that stage LABELS and stage IDS stay on their own sides of
the boundary. The run reasons in labels, HubSpot answers in ids, and a mix-up in either
direction produces a quiet, wrong zero rather than an error.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hubspot_fakes import PIPELINES, FakeHTTP, Response, deal, search_body

from chase import db
from chase.config import load_client
from chase.hubspot import HubSpotClient
from chase.run import Deps, run_ladder
from chase.templates import build_message

NOW = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)  # a Tuesday, 13:00 in Sydney


def hs_date(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def contact(contact_id, email, first, last):
    return Response(
        json_body={
            "id": contact_id,
            "properties": {"email": email, "firstname": first, "lastname": last},
        }
    )


def deal_with_contact(deal_id, stage, days_ago, contact_id, name):
    body = deal(deal_id=deal_id, stage=stage, name=name, quote_sent_at=hs_date(days_ago))
    body["associations"] = {"contacts": {"results": [{"id": contact_id}]}}
    return Response(json_body=body)


# Four quotes: one just sent, one due its first rung, one due its second, and one the
# owner has already won without telling anybody.
SEARCH_RESULTS = [
    deal("1", "s-sent", "Q-0401 website rebuild, Northside Dental", "9800", hs_date(3)),
    deal("2", "s-sent", "Q-0402 brand identity, Corella Coffee", "4200", hs_date(0)),
    deal("3", "s-chasing", "Q-0403 SEO retainer, Lumen Legal", "18000", hs_date(8)),
    deal("4", "s-sent", "Q-0404 ecommerce build, Harbour Cycles", "26000", hs_date(10)),
]

ROUTES = {
    ("GET", "/crm/v3/pipelines/deals"): [Response(json_body=PIPELINES)],
    ("POST", "/crm/v3/objects/deals/search"): [Response(json_body=search_body(SEARCH_RESULTS))],
    ("GET", "/crm/v3/objects/deals/1"): [
        deal_with_contact("1", "s-sent", 3, "c1", "Q-0401 website rebuild, Northside Dental")
    ],
    ("GET", "/crm/v3/objects/deals/3"): [
        deal_with_contact("3", "s-chasing", 8, "c3", "Q-0403 SEO retainer, Lumen Legal")
    ],
    # The fresh read is the whole point of step 7: search still says Sent, the portal
    # says Won.
    ("GET", "/crm/v3/objects/deals/4"): [
        deal_with_contact("4", "s-won", 10, "c4", "Q-0404 ecommerce build, Harbour Cycles")
    ],
    ("GET", "/crm/v3/objects/contacts/c1"): [contact("c1", "ana@northside.example", "Ana", "Diaz")],
    ("GET", "/crm/v3/objects/contacts/c3"): [contact("c3", "sam@lumen.example", "Sam", "Okafor")],
    ("GET", "/crm/v3/objects/contacts/c4"): [contact("c4", "kim@cycles.example", "Kim", "Tan")],
    ("PATCH", "/crm/v3/objects/deals/1"): [Response(json_body={})],
    ("PATCH", "/crm/v3/objects/deals/3"): [Response(json_body={})],
}


class RecordingMail:
    def __init__(self):
        self.sent = []

    def poll_replies(self):
        return []

    def send(self, message):
        self.sent.append(message)


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "data" / "chase.db")
    db.init(connection)
    yield connection
    connection.close()


@pytest.fixture
def client(config_dir):
    return load_client("harbourline", config_dir)


@pytest.fixture
def wired(config_dir):
    """The real client and the real message builder, over a fake transport."""
    repo_root = config_dir.parent
    http = FakeHTTP(ROUTES)
    hubspot = HubSpotClient(token="pat-test", http=http, sleep=lambda _s: None)
    mail = RecordingMail()
    counter = {"n": 0}

    def make_message_id(_client):
        counter["n"] += 1
        return f"<chase-{counter['n']}@harbourline.example>"

    deps = Deps(
        hubspot=hubspot,
        mail=mail,
        build_message=lambda *a: build_message(*a, repo_root=repo_root),
        # The model is never called in a test. Step 1 therefore counts a fallback,
        # which is exactly what the run report should say.
        opener=lambda c, cand, step: ("Hope the week is treating you well.", False),
        make_message_id=make_message_id,
    )
    return deps, http, mail


def test_a_run_over_the_real_client_sends_the_due_rungs(conn, client, wired):
    deps, _http, mail = wired
    report = run_ladder(conn, client, NOW, deps)

    assert report.candidates == 4
    assert report.due == 3  # ages 3, 8 and 10; the one sent today is not due
    assert report.sent == 2
    assert report.stopped == 1  # the deal the portal says is Won
    assert report.status == "ok"
    assert len(mail.sent) == 2


def test_the_messages_reach_the_addresses_the_contact_reads_returned(conn, client, wired):
    deps, _http, mail = wired
    run_ladder(conn, client, NOW, deps)
    recipients = sorted(m["To"] for m in mail.sent)
    assert recipients == ["ana@northside.example", "sam@lumen.example"]
    # The plus address is what makes a reply matchable back to one quote.
    assert sorted(m["Reply-To"] for m in mail.sent) == [
        "quotes+1@harbourline.example",
        "quotes+3@harbourline.example",
    ]


def test_only_the_first_rung_moves_the_stage_and_it_moves_by_id(conn, client, wired):
    deps, http, _mail = wired
    run_ladder(conn, client, NOW, deps)

    first_rung = http.bodies("PATCH", "/crm/v3/objects/deals/1")[0]["properties"]
    assert first_rung["dealstage"] == "s-chasing"  # an id, never the label
    assert first_rung["last_chase_at"] == "2026-09-01T03:00:00Z"

    # Deal 3 was already in Chasing, so the run has no business moving it.
    later_rung = http.bodies("PATCH", "/crm/v3/objects/deals/3")[0]["properties"]
    assert "dealstage" not in later_rung
    assert later_rung["last_chase_at"] == "2026-09-01T03:00:00Z"


def test_a_deal_the_portal_says_is_won_is_stopped_and_never_patched(conn, client, wired):
    deps, http, mail = wired
    run_ladder(conn, client, NOW, deps)

    assert db.is_stopped(conn, "4")
    assert not [c for c in http.calls if c["method"] == "PATCH" and c["path"].endswith("/4")]
    assert "kim@cycles.example" not in [m["To"] for m in mail.sent]


def test_a_second_run_adds_no_rows(conn, client, wired):
    """Idempotence, this time through the real client rather than a stand-in."""
    deps, _http, _mail = wired
    first = run_ladder(conn, client, NOW, deps)
    before = db.counts(conn)["reminder_log"]
    second = run_ladder(conn, client, NOW, deps)

    assert first.sent == 2
    assert second.sent == 0
    assert db.counts(conn)["reminder_log"] == before
    # Deal 4 is still due on paper (nothing was ever claimed for it) and is still
    # refused, which is the stop row doing its job rather than the log doing it.
    assert second.due == 1
    assert second.stopped == 1
