"""The reply poll. Spec sections 4.7 step 3 and 4.8.

An outgoing chase carries `Reply-To: quotes+<deal_id>@<reply_domain>` (chase/templates.py),
so a reply comes back addressed to that plus address and the deal id travels in the
envelope. There is no message-id table to keep in step and no guessing from the sender
address: the spec rules that fallback out because it never fires in a Mailpit demo and
would ship untested.

This is the correctness path, not the fast path. The webhook in the design only decides
whether a reply stops the ladder within seconds or at the next run; if it never fires,
this poll still finds the reply. That is why a poll that will not answer aborts the run
rather than letting it chase blind.
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_BASE_URL = "http://localhost:8025"
REQUEST_TIMEOUT = 10.0

# Mailpit's own search syntax. Narrow on purpose: the ladder only cares about mail
# addressed to a plus address it generated.
SEARCH_QUERY = "to:quotes+"
SEARCH_PATH = "/api/v1/search"

# `quotes+<deal_id>@<domain>`. The deal id is everything between the plus and the at,
# because HubSpot object ids are opaque and a future portal could widen them.
_PLUS_ADDRESS = re.compile(r"^quotes\+([^@]+)@", re.IGNORECASE)


class MailpitError(RuntimeError):
    """A poll Mailpit would not answer. The run turns this into an abort."""


def deal_id_from(address: Any) -> str | None:
    """The deal id in a plus address, or None for anything else.

    None rather than an exception: a human can send anything to that inbox, and an
    address the ladder did not generate is a fact for the run to count, not a crash.
    """
    if not address:
        return None
    match = _PLUS_ADDRESS.match(str(address).strip())
    return match.group(1) if match else None


class MailpitPoller:
    """Reads replies out of Mailpit over its HTTP API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        http: Any = None,
        timeout: float = REQUEST_TIMEOUT,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        if http is None:  # pragma: no cover - the real transport, exercised by the CLI
            import httpx

            http = httpx.Client(timeout=timeout)
        self.http = http

    def poll_replies(self) -> list[dict]:
        """Every plus-addressed message Mailpit is holding, as the run wants them.

        A message whose recipients carry no plus address is returned with `deal_id` of
        None rather than dropped, so the run counts it as an unmatched reply. A reply
        nobody can place is exactly the thing worth seeing in the report.
        """
        data = self._search()
        replies: list[dict] = []
        for msg in data.get("messages") or []:
            replies.append(
                {"deal_id": self._deal_id(msg), "message_id": msg.get("MessageID")}
            )
        return replies

    def delete_all_messages(self) -> None:
        """Empty the inbox, so a demo does not open on last week's chases.

        Mailpit only ever holds mail this project sent to itself, so there is nothing
        here that needs the care `archive_seed` takes on the portal.
        """
        url = f"{self.base_url}/api/v1/messages"
        try:
            response = self.http.request("DELETE", url)
        except Exception as exc:  # noqa: BLE001
            raise MailpitError(f"DELETE /api/v1/messages failed: {exc}") from exc
        if getattr(response, "status_code", 0) >= 400:
            raise MailpitError(f"DELETE /api/v1/messages returned {response.status_code}")

    # -- plumbing -------------------------------------------------------------

    @staticmethod
    def _deal_id(msg: dict) -> str | None:
        for recipient in msg.get("To") or []:
            deal_id = deal_id_from((recipient or {}).get("Address"))
            if deal_id:
                return deal_id
        return None

    def _search(self) -> dict:
        url = f"{self.base_url}{SEARCH_PATH}"
        try:
            response = self.http.request("GET", url, params={"query": SEARCH_QUERY})
        except Exception as exc:  # noqa: BLE001
            raise MailpitError(f"GET {SEARCH_PATH} failed: {exc}") from exc
        if getattr(response, "status_code", 0) >= 400:
            raise MailpitError(f"GET {SEARCH_PATH} returned {response.status_code}")
        try:
            return response.json() or {}
        except Exception:  # noqa: BLE001
            return {}


class Mail:
    """The run wants one collaborator with `send` and `poll_replies`; the two halves
    speak different protocols, SMTP out and HTTP back, so they are built separately and
    joined here."""

    def __init__(self, sender: Any, poller: Any):
        self._sender = sender
        self._poller = poller

    def send(self, message) -> None:
        self._sender.send(message)

    def poll_replies(self) -> list[dict]:
        return self._poller.poll_replies()
