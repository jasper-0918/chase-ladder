"""Sending. Spec sections 4.5 step 8 and 4.8.

The distinction that carries the at-most-once claim lives here. An SMTP call can fail in
two very different ways:

  * the server refused the message before accepting it (connection refused, bad greeting,
    sender or recipient rejected). Nothing was delivered, so the rung is retryable, and
    the run marks it `failed`.

  * anything else, a timeout or a reset. The server may already hold the message. Marking
    that `failed` and retrying it is the one way this system could deliver the same chase
    twice, so it does not: the row stays `claimed` and a human settles it from Mailpit.

`RefusedBeforeDelivery` is the first case, and it is the only exception the run treats as
safe to retry.
"""

from __future__ import annotations

import smtplib
import socket
from email.message import EmailMessage
from email.utils import make_msgid


class RefusedBeforeDelivery(Exception):
    """The server took nothing, so the same rung may be tried again."""


# smtplib exceptions that are raised strictly before the server accepts the body.
_REFUSALS = (
    smtplib.SMTPConnectError,
    smtplib.SMTPHeloError,
    smtplib.SMTPAuthenticationError,
    smtplib.SMTPSenderRefused,
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPNotSupportedError,
    ConnectionRefusedError,
)


def make_message_id(client) -> str:
    """Generated before the send and written by the claim, so a row stuck at `claimed`
    always carries the Message-ID its mail would have used. Spec section 4.4."""
    return make_msgid(domain=client.reply_domain)


class SmtpSender:
    """Sends through Mailpit in the demo, and through any SMTP host in production."""

    def __init__(self, host: str = "localhost", port: int = 1025, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def send(self, message: EmailMessage) -> None:
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                smtp.send_message(message)
        except _REFUSALS as exc:
            raise RefusedBeforeDelivery(str(exc)) from exc
        except (socket.timeout, TimeoutError):
            # Ambiguous on purpose: let it propagate, the caller keeps the row `claimed`.
            raise
