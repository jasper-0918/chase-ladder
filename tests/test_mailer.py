"""Sending. Spec sections 4.5 step 8 and 4.8.

One distinction carries the whole at-most-once claim, and it lives in this module: did
the server refuse the message before taking it, or might it already hold a copy? The
first is retryable and the run marks it `failed`. The second is ambiguous, the row stays
`claimed`, and a human settles it. Getting that classification wrong is the only way
this system could send the same chase twice, so every branch is asserted here.

No real socket is opened. `smtplib.SMTP` is replaced by a double that records what it
was asked to do.
"""

from __future__ import annotations

import smtplib
import socket
from email.message import EmailMessage

import pytest

from chase import mailer
from chase.mailer import RefusedBeforeDelivery, SmtpSender, make_message_id


class FakeSMTP:
    """Stands in for smtplib.SMTP, including its context-manager shape."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sent: list[EmailMessage] = []
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.quit_called = True
        return False

    def send_message(self, message):
        self.sent.append(message)


@pytest.fixture(autouse=True)
def fresh_instances():
    FakeSMTP.instances = []
    yield
    FakeSMTP.instances = []


@pytest.fixture
def smtp(monkeypatch):
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def a_message() -> EmailMessage:
    message = EmailMessage()
    message["To"] = "customer@customer.example"
    message["From"] = "quotes@harbourline.example"
    message["Subject"] = "Following up on your quote"
    message.set_content("Just checking in.")
    return message


def raising(exc):
    """A FakeSMTP subclass whose send_message raises."""

    class Raising(FakeSMTP):
        def send_message(self, message):
            raise exc

    return Raising


# --- the happy path ----------------------------------------------------------


def test_a_send_reaches_the_configured_host_and_port(smtp):
    SmtpSender(host="mailpit.internal", port=1025, timeout=3.0).send(a_message())
    served = FakeSMTP.instances[0]
    assert (served.host, served.port, served.timeout) == ("mailpit.internal", 1025, 3.0)
    assert len(served.sent) == 1


def test_the_connection_is_closed_even_though_nothing_asks_it_to(smtp):
    """The `with` block is the only thing that quits the session."""
    SmtpSender().send(a_message())
    assert FakeSMTP.instances[0].quit_called


def test_the_defaults_point_at_mailpit(smtp):
    SmtpSender().send(a_message())
    served = FakeSMTP.instances[0]
    assert (served.host, served.port) == ("localhost", 1025)


# --- refused before delivery: retryable --------------------------------------
# Nothing was delivered, so the run marks the rung `failed` and the next run tries it
# again. Every exception here is raised strictly before the server accepts the body.


@pytest.mark.parametrize(
    "exc",
    [
        smtplib.SMTPConnectError(421, "cannot connect"),
        smtplib.SMTPHeloError(501, "bad greeting"),
        smtplib.SMTPAuthenticationError(535, "bad credentials"),
        smtplib.SMTPSenderRefused(553, "sender rejected", "quotes@harbourline.example"),
        smtplib.SMTPRecipientsRefused({"customer@customer.example": (550, b"no such user")}),
        smtplib.SMTPNotSupportedError("STARTTLS not supported"),
        ConnectionRefusedError("nothing listening on 1025"),
    ],
    ids=["connect", "helo", "auth", "sender", "recipients", "unsupported", "refused"],
)
def test_a_refusal_becomes_the_one_exception_the_run_may_retry(monkeypatch, exc):
    monkeypatch.setattr(mailer.smtplib, "SMTP", raising(exc))
    with pytest.raises(RefusedBeforeDelivery):
        SmtpSender().send(a_message())


def test_the_refusal_keeps_the_original_reason(monkeypatch):
    """A run that reports `failed` with no detail is a run nobody can debug."""
    monkeypatch.setattr(mailer.smtplib, "SMTP", raising(ConnectionRefusedError("port 1025 shut")))
    with pytest.raises(RefusedBeforeDelivery, match="1025"):
        SmtpSender().send(a_message())


# --- ambiguous: never retried ------------------------------------------------
# The server may already hold the message. These must NOT become RefusedBeforeDelivery,
# because the run would then retry a chase the customer has already received.


@pytest.mark.parametrize(
    "exc",
    [
        socket.timeout("timed out after DATA"),
        TimeoutError("timed out after DATA"),
        smtplib.SMTPServerDisconnected("connection reset"),
        smtplib.SMTPDataError(451, "try again later"),
    ],
    ids=["socket-timeout", "timeout", "disconnected", "data-error"],
)
def test_an_ambiguous_failure_is_not_dressed_up_as_retryable(monkeypatch, exc):
    monkeypatch.setattr(mailer.smtplib, "SMTP", raising(exc))
    with pytest.raises(Exception) as caught:
        SmtpSender().send(a_message())
    assert not isinstance(caught.value, RefusedBeforeDelivery)


# --- the message id ----------------------------------------------------------


def test_the_message_id_is_stamped_with_the_clients_reply_domain():
    class Client:
        reply_domain = "harbourline.example"

    generated = make_message_id(Client())
    assert generated.startswith("<") and generated.endswith(">")
    assert generated.endswith("@harbourline.example>")


def test_two_message_ids_are_never_the_same():
    class Client:
        reply_domain = "harbourline.example"

    assert make_message_id(Client()) != make_message_id(Client())
