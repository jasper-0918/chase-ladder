"""The HubSpot client. Spec sections 4.5 and 4.7.

HubSpot is the only place quotes live, so this module is the boundary between the
ladder and the outside world. It implements exactly the four methods the run declares
in its `HubSpot` protocol and nothing else.

One rule runs through the whole file: **labels never reach the wire.** The client YAML
speaks in labels (Sent, Chasing, Won, Lost) because a human maintains it, and HubSpot's
`dealstage` holds a stage *id*. The pipeline is read once, a label-to-id map is built
from it, and every filter, comparison and PATCH uses the id. Renaming a stage in the
portal is therefore free; the ids do not move.

The transport is injected. Tests pass a fake that records what was asked of it, so the
whole of this file is exercised with no network and no mocking library.

Rate limits: Free allows 100 requests per 10 seconds and 250,000 a day, and one run is
one search, one read and one contact read per due deal, plus a PATCH per send. A 429 is
slept off once against `Retry-After` and then abandoned, because a run that half
updated the portal is worse than one that reports it stopped.
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timezone
from typing import Any

import httpx

from chase.config import Client
from chase.ladder import Candidate

BASE_URL = "https://api.hubapi.com"

# The properties the ladder needs and no others. `quote_sent_at` is the one the whole
# schedule hangs off; HubSpot's own "date entered stage" is read-only and would say the
# seed time for every deal.
SEARCH_PROPERTIES = ("dealname", "amount", "dealstage", "quote_sent_at", "last_chase_at")
CONTACT_PROPERTIES = ("email", "firstname", "lastname")

SEARCH_LIMIT = 200  # one page covers the demo; paging is not implemented on purpose
REQUEST_TIMEOUT = 10.0

# The only stage the ladder moves a deal out of after a send, and the one the run has
# to still find there for that move to be safe. Anything else, Chasing already, dragged
# back to Draft, or closed by the customer mid-send, is left exactly where it is.
MOVABLE_FROM_STAGE = "Sent"

# What to wait when a 429 arrives with no Retry-After header. The documented Free
# window is 100 requests per 10 seconds, so 10 seconds is the honest guess.
DEFAULT_RETRY_AFTER = 10.0


class HubSpotError(RuntimeError):
    """A call HubSpot would not answer. The run turns this into an abort."""


class UnknownStage(HubSpotError):
    """A stage label in the YAML that the portal's pipeline does not carry.

    Raised rather than passed through, because sending a label where HubSpot expects an
    id fails silently: the search matches nothing and the run reports a quiet zero.
    """


def parse_hs_datetime(raw: Any) -> datetime | None:
    """Read a HubSpot date or datetime property, always as UTC.

    Three shapes are accepted because HubSpot has returned all three across its APIs and
    property types: an ISO date, an ISO timestamp (with `Z`, an offset, or fractional
    seconds), and epoch milliseconds. Which one this portal actually returns for a Date
    property is confirmed by the live spike, not by this parser; accepting all three
    means the answer cannot break the run either way.

    Anything unusable is None rather than an exception, because a missing date is a
    business fact (a deal nobody quoted) and the callers decide what it means.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)

    text = str(raw).strip()
    if not text:
        return None
    # 10+ digits is epoch milliseconds; a bare year is not, hence the length guard.
    if text.isdigit() and len(text) >= 10:
        return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def format_hs_datetime(value: datetime) -> str:
    """UTC, to the second, with a literal Z. The shape HubSpot accepts on a write."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class HubSpotClient:
    """Implements the run's `HubSpot` protocol.

    Constructing one makes no network call, so `doctor` and the CLI can build it before
    they know whether the portal is reachable. The pipeline read happens on first use.
    """

    def __init__(
        self,
        token: str,
        http: Any | None = None,
        base_url: str = BASE_URL,
        sleep: Any = _time.sleep,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        # One client per process. Tests pass a fake and never touch httpx.
        self.http = http if http is not None else httpx.Client(timeout=REQUEST_TIMEOUT)
        self.sleep = sleep
        self.pipeline_id: str | None = None
        self._label_to_id: dict[str, str] | None = None
        self._id_to_label: dict[str, str] = {}
        # Deals that carry no quote date, so no rung can ever be due for them. Kept
        # rather than counted so `doctor` can name the offending records.
        self.skipped_no_quote_date: list[str] = []

    # -- the stage map --------------------------------------------------------

    @property
    def stage_map(self) -> dict[str, str]:
        if self._label_to_id is None:
            self._load_pipeline()
        assert self._label_to_id is not None
        return self._label_to_id

    def _load_pipeline(self) -> None:
        data = self._request("GET", "/crm/v3/pipelines/deals")
        results = data.get("results") or []
        if not results:
            raise HubSpotError("the portal has no deal pipeline to read stages from")
        # Free allows exactly one deal pipeline, which is why both demo clients share it.
        pipeline = results[0]
        self.pipeline_id = pipeline.get("id")
        stages = pipeline.get("stages") or []
        self._label_to_id = {s["label"]: s["id"] for s in stages if "label" in s and "id" in s}
        self._id_to_label = {v: k for k, v in self._label_to_id.items()}

    def stage_id(self, label: str) -> str:
        try:
            return self.stage_map[label]
        except KeyError:
            known = ", ".join(sorted(self.stage_map)) or "none"
            raise UnknownStage(
                f"the pipeline has no stage labelled {label!r}; it carries: {known}"
            ) from None

    def stage_label(self, stage_id: str | None) -> str | None:
        return self._id_to_label.get(stage_id) if stage_id else None

    # -- the protocol ---------------------------------------------------------

    def search_candidates(self, client: Client) -> list[Candidate]:
        """Every deal sitting in Sent or Chasing, as the ladder sees it.

        The search carries no associations, so `contact_email` is unset here; the direct
        read in step 7 of the run is what supplies it, on the deals that turn out to be
        due.
        """
        stage_ids = [self.stage_id("Sent"), self.stage_id("Chasing")]
        body = {
            "filterGroups": [
                {"filters": [{"propertyName": "dealstage", "operator": "IN", "values": stage_ids}]}
            ],
            "properties": list(SEARCH_PROPERTIES),
            "limit": SEARCH_LIMIT,
        }
        data = self._request("POST", "/crm/v3/objects/deals/search", json=body)

        self.skipped_no_quote_date = []
        candidates: list[Candidate] = []
        for result in data.get("results") or []:
            props = result.get("properties") or {}
            label = self.stage_label(props.get("dealstage"))
            if label is None:
                # A stage outside the pipeline we read. Not ours to chase.
                continue
            quote_sent_at = parse_hs_datetime(props.get("quote_sent_at"))
            if quote_sent_at is None:
                # A deal someone made by hand. Dropping it is right; dropping it
                # silently is not, so the id is kept for doctor.
                self.skipped_no_quote_date.append(result.get("id"))
                continue
            candidates.append(
                Candidate(
                    deal_id=str(result.get("id")),
                    name=props.get("dealname") or "",
                    amount=_as_amount(props.get("amount")),
                    stage=label,
                    quote_sent_at=quote_sent_at,
                    last_chase_at=parse_hs_datetime(props.get("last_chase_at")),
                )
            )
        return candidates

    def read_deal(self, deal_id: str) -> Candidate:
        """One deal, fresh, with its contact resolved.

        The run calls this immediately before a claim because search results lag writes
        by a few moments, and a stale stage is the one thing that could chase a quote the
        owner has already closed.
        """
        self.stage_map  # ensure the pipeline is loaded before any label lookup
        data = self._request(
            "GET",
            f"/crm/v3/objects/deals/{deal_id}",
            params={"associations": "contacts", "properties": ",".join(SEARCH_PROPERTIES)},
        )
        props = data.get("properties") or {}

        quote_sent_at = parse_hs_datetime(props.get("quote_sent_at"))
        if quote_sent_at is None:
            # It had one moments ago, in the search. Someone cleared it mid-run, so the
            # safe move is to stop rather than to guess a date and chase on it.
            raise HubSpotError(f"deal {deal_id} has no quote_sent_at on the direct read")

        stage_id = props.get("dealstage")
        # An unmapped id is passed through as itself: the run treats any stage outside
        # Sent, Chasing and the stop stages as "not ours this run" and writes nothing.
        stage = self.stage_label(stage_id) or str(stage_id)

        contact_email, contact_name = self._read_associated_contact(data)
        return Candidate(
            deal_id=str(data.get("id", deal_id)),
            name=props.get("dealname") or "",
            amount=_as_amount(props.get("amount")),
            stage=stage,
            quote_sent_at=quote_sent_at,
            last_chase_at=parse_hs_datetime(props.get("last_chase_at")),
            contact_email=contact_email,
            contact_name=contact_name,
        )

    def patch_after_send(
        self, deal_id: str, last_chase_at: datetime, stage: str | None = None
    ) -> None:
        """Stamp the chase and, on the first rung only, move Sent to Chasing.

        The stage the run decided on was read before the message went out, and the send
        leaves the SQLite transaction. A customer can accept, or the owner can close the
        deal, while it is in flight. Writing that decision back blind is how a Won deal
        was dragged to Chasing and then chased on every later run, so the move is
        confirmed against a fresh read and dropped unless the deal is still where the run
        left it. A read that will not answer is not permission to move it either.

        The stamp always lands. It records that a message went out, which is true however
        the stage has changed.
        """
        properties: dict[str, str] = {"last_chase_at": format_hs_datetime(last_chase_at)}
        if stage is not None and self._still_in(deal_id, MOVABLE_FROM_STAGE):
            properties["dealstage"] = self.stage_id(stage)
        self._request("PATCH", f"/crm/v3/objects/deals/{deal_id}", json={"properties": properties})

    def _still_in(self, deal_id: str, expected: str) -> bool:
        """Re-read one deal's stage. False on anything but a clear match."""
        self.stage_map  # ensure the pipeline is loaded before any label lookup
        try:
            data = self._request(
                "GET",
                f"/crm/v3/objects/deals/{deal_id}",
                params={"properties": "dealstage"},
            )
        except Exception:  # noqa: BLE001
            return False
        return self.stage_label((data.get("properties") or {}).get("dealstage")) == expected

    def patch_stage(self, deal_id: str, stage: str) -> None:
        """Move a deal, used to project a reply onto the board as Replied."""
        self._request(
            "PATCH",
            f"/crm/v3/objects/deals/{deal_id}",
            json={"properties": {"dealstage": self.stage_id(stage)}},
        )

    # -- plumbing -------------------------------------------------------------

    def _read_associated_contact(self, deal: dict) -> tuple[str | None, str | None]:
        associations = (deal.get("associations") or {}).get("contacts") or {}
        results = associations.get("results") or []
        if not results:
            return None, None
        contact_id = results[0].get("id")
        if not contact_id:
            return None, None
        contact = self._request(
            "GET",
            f"/crm/v3/objects/contacts/{contact_id}",
            params={"properties": ",".join(CONTACT_PROPERTIES)},
        )
        props = contact.get("properties") or {}
        email = props.get("email") or None
        name = " ".join(p for p in (props.get("firstname"), props.get("lastname")) if p)
        return email, name or None

    def _request(
        self, method: str, path: str, json: dict | None = None, params: dict | None = None
    ) -> dict:
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        response = self.http.request(method, url, json=json, params=params, headers=headers)
        if response.status_code == 429:
            # Once. A second 429 means the budget is genuinely gone and the run should
            # say so rather than sit in a loop holding claimed rows.
            self.sleep(_retry_after(response))
            response = self.http.request(method, url, json=json, params=params, headers=headers)
        if response.status_code >= 400:
            raise _error(method, path, response)
        try:
            return response.json()
        except Exception:  # noqa: BLE001
            return {}


def _as_amount(raw: Any) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _retry_after(response: Any) -> float:
    raw = (getattr(response, "headers", None) or {}).get("Retry-After")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER


def _error(method: str, path: str, response: Any) -> HubSpotError:
    """Carry HubSpot's own words through.

    A 403 body names the scopes the endpoint accepts, which is the fastest way to fix a
    private app that is missing one, so it is quoted rather than summarised.
    """
    detail = ""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = None
    if isinstance(body, dict):
        parts = [str(body.get("message") or "")]
        if body.get("context"):
            parts.append(str(body["context"]))
        if body.get("category"):
            parts.append(str(body["category"]))
        detail = " ".join(p for p in parts if p)
    if not detail:
        detail = str(getattr(response, "text", "") or "")
    return HubSpotError(f"{method} {path} returned {response.status_code}: {detail}")
