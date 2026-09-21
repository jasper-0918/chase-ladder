"""The HubSpot client. Spec sections 4.5 and 4.7.

No network and no mocking library: a fake transport stands in for httpx.Client and
records what was asked of it, the same pattern the run tests use for their fakes. The
assertions that matter are about ids, not labels. HubSpot stores a stage id in
"dealstage", the client YAML holds labels, and every bug this file guards against is
one where a label reached the wire.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from hubspot_fakes import PIPELINES, FakeHTTP, FakeSleep, Response, deal, search_body

from chase.config import load_client
from chase.hubspot import HubSpotClient, HubSpotError, UnknownStage, parse_hs_datetime


@pytest.fixture
def client():
    return load_client("harbourline", config_dir="config")


def make(routes, sleep=None):
    http = FakeHTTP(routes)
    return HubSpotClient(token="pat-test", http=http, sleep=sleep or FakeSleep()), http


# --- the stage map -----------------------------------------------------------


def test_constructing_the_client_makes_no_call():
    """doctor and the CLI build this before they know the network is up."""
    _hs, http = make({})
    assert http.calls == []


def test_stage_map_reads_pipelines_once(client):
    routes = {
        ("GET", "/crm/v3/pipelines/deals"): [Response(json_body=PIPELINES)],
        ("POST", "/crm/v3/objects/deals/search"): [Response(json_body=search_body([]))],
    }
    hs, http = make(routes)
    hs.search_candidates(client)
    hs.search_candidates(client)
    pipeline_reads = [c for c in http.calls if c["path"] == "/crm/v3/pipelines/deals"]
    assert len(pipeline_reads) == 1


def test_unknown_stage_label_raises_rather_than_sending_the_label():
    thin = {"results": [{"id": "default", "stages": [{"id": "s-sent", "label": "Sent"}]}]}
    routes = {("GET", "/crm/v3/pipelines/deals"): [Response(json_body=thin)]}
    hs, _ = make(routes)
    with pytest.raises(UnknownStage) as exc:
        hs.patch_stage("1", "Replied")
    assert "Replied" in str(exc.value)


# --- search ------------------------------------------------------------------


def test_search_filters_by_stage_id_not_label(client):
    routes = {
        ("GET", "/crm/v3/pipelines/deals"): [Response(json_body=PIPELINES)],
        ("POST", "/crm/v3/objects/deals/search"): [Response(json_body=search_body([]))],
    }
    hs, http = make(routes)
    hs.search_candidates(client)
    body = [c for c in http.calls if c["path"].endswith("/search")][0]["json"]
    values = body["filterGroups"][0]["filters"][0]["values"]
    assert values == ["s-sent", "s-chasing"]
    assert "Sent" not in values and "Chasing" not in values
    assert set(body["properties"]) == {
        "dealname",
        "amount",
        "dealstage",
        "quote_sent_at",
        "last_chase_at",
    }


def test_search_maps_results_into_candidates(client):
    results = [
        deal(
            deal_id="7",
            stage="s-chasing",
            amount="12000.50",
            quote_sent_at="2026-08-20",
            last_chase_at="2026-08-23T04:00:00Z",
        )
    ]
    routes = {
        ("GET", "/crm/v3/pipelines/deals"): [Response(json_body=PIPELINES)],
        ("POST", "/crm/v3/objects/deals/search"): [Response(json_body=search_body(results))],
    }
    hs, _ = make(routes)
    got = hs.search_candidates(client)
    assert len(got) == 1
    candidate = got[0]
    assert candidate.deal_id == "7"
    assert candidate.stage == "Chasing"  # the id came back as a label
    assert candidate.amount == 12000.50
    assert candidate.quote_sent_at == datetime(2026, 8, 20, tzinfo=timezone.utc)
    assert candidate.last_chase_at == datetime(2026, 8, 23, 4, 0, tzinfo=timezone.utc)
    assert candidate.contact_email is None  # search carries no associations


def test_search_skips_a_deal_with_no_quote_sent_at_and_records_it(client):
    """A deal someone made by hand has no quote date, so no rung can be due for it.

    Dropping it is right, dropping it silently is not: one bad record must not abort a
    run, and it must not vanish either, so the id is kept for doctor to print.
    """
    results = [deal(deal_id="7"), deal(deal_id="8", quote_sent_at=None)]
    routes = {
        ("GET", "/crm/v3/pipelines/deals"): [Response(json_body=PIPELINES)],
        ("POST", "/crm/v3/objects/deals/search"): [Response(json_body=search_body(results))],
    }
    hs, _ = make(routes)
    got = hs.search_candidates(client)
    assert [c.deal_id for c in got] == ["7"]
    assert hs.skipped_no_quote_date == ["8"]


def test_search_skips_a_deal_whose_stage_id_is_not_in_the_map(client):
    """A stage the pipeline read does not know about is not ours to chase."""
    results = [deal(deal_id="9", stage="s-unmapped")]
    routes = {
        ("GET", "/crm/v3/pipelines/deals"): [Response(json_body=PIPELINES)],
        ("POST", "/crm/v3/objects/deals/search"): [Response(json_body=search_body(results))],
    }
    hs, _ = make(routes)
    assert hs.search_candidates(client) == []


# --- the direct read ---------------------------------------------------------


def test_read_deal_asks_for_associations_and_resolves_the_contact():
    routes = {
        ("GET", "/crm/v3/pipelines/deals"): [Response(json_body=PIPELINES)],
        ("GET", "/crm/v3/objects/deals/7"): [
            Response(
                json_body={
                    **deal(deal_id="7", stage="s-won"),
                    "associations": {
                        "contacts": {"results": [{"id": "c1", "type": "deal_to_contact"}]}
                    },
                }
            )
        ],
        ("GET", "/crm/v3/objects/contacts/c1"): [
            Response(
                json_body={
                    "id": "c1",
                    "properties": {
                        "email": "pat@buyer.example",
                        "firstname": "Pat",
                        "lastname": "Ng",
                    },
                }
            )
        ],
    }
    hs, http = make(routes)
    got = hs.read_deal("7")
    assert got.stage == "Won"
    assert got.contact_email == "pat@buyer.example"
    assert got.contact_name == "Pat Ng"
    read = [c for c in http.calls if c["path"] == "/crm/v3/objects/deals/7"][0]
    assert read["params"]["associations"] == "contacts"


def test_read_deal_with_no_associated_contact_leaves_the_email_unset():
    routes = {
        ("GET", "/crm/v3/pipelines/deals"): [Response(json_body=PIPELINES)],
        ("GET", "/crm/v3/objects/deals/7"): [
            Response(json_body={**deal(deal_id="7"), "associations": {}})
        ],
    }
    hs, http = make(routes)
    got = hs.read_deal("7")
    assert got.contact_email is None
    assert not [c for c in http.calls if "contacts" in c["path"]]


# --- the writes --------------------------------------------------------------


def test_patch_after_send_writes_the_timestamp_and_the_stage_id():
    routes = {
        ("GET", "/crm/v3/pipelines/deals"): [Response(json_body=PIPELINES)],
        ("GET", "/crm/v3/objects/deals/7"): [Response(json_body=deal(deal_id="7"))],
        ("PATCH", "/crm/v3/objects/deals/7"): [Response(json_body={})],
    }
    hs, http = make(routes)
    hs.patch_after_send(
        "7", last_chase_at=datetime(2026, 9, 7, 12, 30, tzinfo=timezone.utc), stage="Chasing"
    )
    props = [c for c in http.calls if c["method"] == "PATCH"][0]["json"]["properties"]
    assert props["last_chase_at"] == "2026-09-07T12:30:00Z"
    assert props["dealstage"] == "s-chasing"


# The send leaves the SQLite transaction, so a customer can accept, or the owner can
# close the deal, while the message is in flight. The stage the run decided on was read
# before that window. Writing it back blind is how a Won deal was dragged to Chasing and
# chased again on every later run.


@pytest.mark.parametrize("closed_stage", ["s-won", "s-lost", "s-replied"])
def test_patch_after_send_leaves_a_deal_that_closed_during_the_send(closed_stage):
    routes = {
        ("GET", "/crm/v3/pipelines/deals"): [Response(json_body=PIPELINES)],
        ("GET", "/crm/v3/objects/deals/7"): [
            Response(json_body=deal(deal_id="7", stage=closed_stage))
        ],
        ("PATCH", "/crm/v3/objects/deals/7"): [Response(json_body={})],
    }
    hs, http = make(routes)
    hs.patch_after_send(
        "7", last_chase_at=datetime(2026, 9, 7, 12, 30, tzinfo=timezone.utc), stage="Chasing"
    )
    props = [c for c in http.calls if c["method"] == "PATCH"][0]["json"]["properties"]
    assert props["last_chase_at"] == "2026-09-07T12:30:00Z"
    assert "dealstage" not in props


def test_patch_after_send_still_stamps_when_the_confirming_read_fails():
    """A read that will not answer is not permission to move the deal."""
    routes = {
        ("GET", "/crm/v3/pipelines/deals"): [Response(json_body=PIPELINES)],
        ("GET", "/crm/v3/objects/deals/7"): [Response(status_code=500)],
        ("PATCH", "/crm/v3/objects/deals/7"): [Response(json_body={})],
    }
    hs, http = make(routes)
    hs.patch_after_send(
        "7", last_chase_at=datetime(2026, 9, 7, 12, 30, tzinfo=timezone.utc), stage="Chasing"
    )
    props = [c for c in http.calls if c["method"] == "PATCH"][0]["json"]["properties"]
    assert props["last_chase_at"] == "2026-09-07T12:30:00Z"
    assert "dealstage" not in props


def test_patch_after_send_without_a_stage_makes_no_confirming_read():
    """Nothing to guard, so the extra call would be waste on every later rung."""
    routes = {
        ("GET", "/crm/v3/pipelines/deals"): [Response(json_body=PIPELINES)],
        ("PATCH", "/crm/v3/objects/deals/7"): [Response(json_body={})],
    }
    hs, http = make(routes)
    hs.patch_after_send(
        "7", last_chase_at=datetime(2026, 9, 7, 12, 30, tzinfo=timezone.utc), stage=None
    )
    assert not [c for c in http.calls if c["path"] == "/crm/v3/objects/deals/7" and c["method"] == "GET"]


def test_patch_after_send_without_a_stage_leaves_the_stage_alone():
    routes = {
        ("GET", "/crm/v3/pipelines/deals"): [Response(json_body=PIPELINES)],
        ("PATCH", "/crm/v3/objects/deals/7"): [Response(json_body={})],
    }
    hs, http = make(routes)
    hs.patch_after_send(
        "7", last_chase_at=datetime(2026, 9, 7, 12, 30, tzinfo=timezone.utc), stage=None
    )
    props = [c for c in http.calls if c["method"] == "PATCH"][0]["json"]["properties"]
    assert "dealstage" not in props


def test_patch_stage_sends_the_id():
    routes = {
        ("GET", "/crm/v3/pipelines/deals"): [Response(json_body=PIPELINES)],
        ("PATCH", "/crm/v3/objects/deals/7"): [Response(json_body={})],
    }
    hs, http = make(routes)
    hs.patch_stage("7", "Replied")
    props = [c for c in http.calls if c["method"] == "PATCH"][0]["json"]["properties"]
    assert props == {"dealstage": "s-replied"}


# --- rate limiting and errors ------------------------------------------------


def test_429_is_retried_once_after_retry_after(client):
    routes = {
        ("GET", "/crm/v3/pipelines/deals"): [Response(json_body=PIPELINES)],
        ("POST", "/crm/v3/objects/deals/search"): [
            Response(status_code=429, headers={"Retry-After": "3"}),
            Response(json_body=search_body([])),
        ],
    }
    sleep = FakeSleep()
    hs, http = make(routes, sleep=sleep)
    hs.search_candidates(client)
    assert sleep.slept == [3.0]
    assert len([c for c in http.calls if c["path"].endswith("/search")]) == 2


def test_a_second_429_aborts_rather_than_looping(client):
    routes = {
        ("GET", "/crm/v3/pipelines/deals"): [Response(json_body=PIPELINES)],
        ("POST", "/crm/v3/objects/deals/search"): [
            Response(status_code=429, headers={"Retry-After": "1"}),
            Response(status_code=429, headers={"Retry-After": "1"}),
        ],
    }
    hs, _ = make(routes)
    with pytest.raises(HubSpotError):
        hs.search_candidates(client)


def test_a_429_with_no_retry_after_uses_the_documented_window(client):
    """The Free limit is 100 requests per 10 seconds, so 10 s is the honest wait."""
    routes = {
        ("GET", "/crm/v3/pipelines/deals"): [Response(json_body=PIPELINES)],
        ("POST", "/crm/v3/objects/deals/search"): [
            Response(status_code=429),
            Response(json_body=search_body([])),
        ],
    }
    sleep = FakeSleep()
    hs, _ = make(routes, sleep=sleep)
    hs.search_candidates(client)
    assert sleep.slept == [10.0]


def test_403_names_the_scope_hubspot_wanted(client):
    """The 403 body lists the accepted scopes, so it goes into the message verbatim."""
    body = {
        "message": "This app has not been granted all required scopes",
        "context": {"requiredScopes": ["crm.objects.deals.read"]},
    }
    routes = {("GET", "/crm/v3/pipelines/deals"): [Response(status_code=403, json_body=body)]}
    hs, _ = make(routes)
    with pytest.raises(HubSpotError) as exc:
        hs.search_candidates(client)
    assert "crm.objects.deals.read" in str(exc.value)


def test_the_token_travels_as_a_bearer_header(client):
    routes = {
        ("GET", "/crm/v3/pipelines/deals"): [Response(json_body=PIPELINES)],
        ("POST", "/crm/v3/objects/deals/search"): [Response(json_body=search_body([]))],
    }
    hs, http = make(routes)
    hs.search_candidates(client)
    assert http.calls[0]["headers"]["Authorization"] == "Bearer pat-test"


# --- property parsing --------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-08-20", datetime(2026, 8, 20, tzinfo=timezone.utc)),
        ("2026-08-20T04:30:00Z", datetime(2026, 8, 20, 4, 30, tzinfo=timezone.utc)),
        ("2026-08-20T04:30:00.000Z", datetime(2026, 8, 20, 4, 30, tzinfo=timezone.utc)),
        ("2026-08-20T04:30:00+00:00", datetime(2026, 8, 20, 4, 30, tzinfo=timezone.utc)),
        (1787200200000, datetime(2026, 8, 20, 4, 30, tzinfo=timezone.utc)),
        ("1787200200000", datetime(2026, 8, 20, 4, 30, tzinfo=timezone.utc)),
    ],
)
def test_parse_hs_datetime_accepts_every_shape_hubspot_returns(raw, expected):
    assert parse_hs_datetime(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "not a date"])
def test_parse_hs_datetime_returns_none_for_nothing_usable(raw):
    assert parse_hs_datetime(raw) is None
