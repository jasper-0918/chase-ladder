"""The reply poll. Spec sections 4.7 step 3 and 4.8.

The deal id travels in the envelope, not in a lookup table: an outgoing chase carries
`Reply-To: quotes+<deal_id>@<reply_domain>`, so a reply comes back addressed to that
plus address. These tests guard the parse, because a reply the poll cannot match is a
ladder that keeps chasing someone who already answered.
"""

from __future__ import annotations

import pytest

from chase.mailpit import MailpitError, MailpitPoller, deal_id_from


class Response:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = str(self._json)

    def json(self):
        return self._json


class FakeHTTP:
    def __init__(self, response=None, raises=None):
        self.response = response or Response()
        self.raises = raises
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "params": kwargs.get("params")})
        if self.raises:
            raise self.raises
        return self.response


def message(message_id="<r1@customer.example>", to=("quotes+7@harbourline.example",)):
    return {
        "ID": "mailpit-internal-id",
        "MessageID": message_id,
        "From": {"Address": "customer@customer.example"},
        "To": [{"Address": a} for a in to],
        "Subject": "Re: your quote",
    }


def make(messages=None, status_code=200, raises=None):
    body = {"messages": list(messages or [])}
    http = FakeHTTP(Response(status_code=status_code, json_body=body), raises=raises)
    return MailpitPoller(base_url="http://localhost:8025", http=http), http


# --- the parse ---------------------------------------------------------------


@pytest.mark.parametrize(
    "address,expected",
    [
        ("quotes+7@harbourline.example", "7"),
        ("quotes+abc-123@lakeshorefitout.example", "abc-123"),
        ("QUOTES+7@Harbourline.Example", "7"),
        ("  quotes+7@harbourline.example  ", "7"),
        ("quotes@harbourline.example", None),
        ("hello@harbourline.example", None),
        ("", None),
        (None, None),
    ],
)
def test_deal_id_from_reads_only_a_plus_address(address, expected):
    assert deal_id_from(address) == expected


# --- the poll ----------------------------------------------------------------


def test_the_poll_asks_mailpit_for_plus_addressed_mail():
    poller, http = make()
    poller.poll_replies()
    call = http.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "http://localhost:8025/api/v1/search"
    assert call["params"] == {"query": "to:quotes+"}


def test_a_reply_carries_its_deal_id_and_its_own_message_id():
    poller, _ = make([message(message_id="<r9@customer.example>", to=("quotes+7@h.example",))])
    assert poller.poll_replies() == [{"deal_id": "7", "message_id": "<r9@customer.example>"}]


def test_the_plus_address_is_found_among_several_recipients():
    poller, _ = make(
        [message(to=("team@harbourline.example", "quotes+42@harbourline.example"))]
    )
    assert poller.poll_replies()[0]["deal_id"] == "42"


def test_a_reply_with_no_plus_address_is_reported_rather_than_dropped():
    """The run counts it as unmatched. Swallowing it here would hide a real reply."""
    poller, _ = make([message(to=("team@harbourline.example",))])
    assert poller.poll_replies() == [{"deal_id": None, "message_id": "<r1@customer.example>"}]


def test_an_empty_inbox_is_an_empty_list():
    poller, _ = make([])
    assert poller.poll_replies() == []


def test_a_missing_messages_key_is_an_empty_list():
    http = FakeHTTP(Response(json_body={}))
    assert MailpitPoller(base_url="http://localhost:8025", http=http).poll_replies() == []


# --- failure -----------------------------------------------------------------
# The run turns any of these into RunAborted. Chasing without the ability to see
# replies is the one thing the design refuses to do.


def test_an_http_error_raises():
    poller, _ = make(status_code=500)
    with pytest.raises(MailpitError):
        poller.poll_replies()


def test_an_unreachable_mailpit_raises():
    poller, _ = make(raises=ConnectionRefusedError("no mailpit on 8025"))
    with pytest.raises(MailpitError):
        poller.poll_replies()


# --- emptying the inbox ------------------------------------------------------


def test_delete_all_asks_mailpit_to_empty_itself():
    poller, http = make()
    poller.delete_all_messages()
    call = http.calls[0]
    assert call["method"] == "DELETE"
    assert call["url"] == "http://localhost:8025/api/v1/messages"


def test_delete_all_raises_when_mailpit_refuses():
    poller, _ = make(status_code=500)
    with pytest.raises(MailpitError):
        poller.delete_all_messages()


def test_delete_all_raises_when_mailpit_is_unreachable():
    poller, _ = make(raises=ConnectionRefusedError("no mailpit on 8025"))
    with pytest.raises(MailpitError):
        poller.delete_all_messages()


def test_a_trailing_slash_on_the_base_url_does_not_double_up():
    http = FakeHTTP(Response(json_body={"messages": []}))
    MailpitPoller(base_url="http://localhost:8025/", http=http).poll_replies()
    assert http.calls[0]["url"] == "http://localhost:8025/api/v1/search"
