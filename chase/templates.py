"""Message building. Spec sections 4.5 step 8 and 4.9.

Amounts, dates and customer names are inserted HERE, after any model call, as literal
strings from the data. Nothing that carries a commitment is ever generated.
"""

from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from chase.config import Client, Step
from chase.ladder import Candidate

QUOTE_LABEL = re.compile(r"^(Q-\d+)")


def quote_label(candidate: Candidate) -> str:
    """The human label at the front of the deal name, for subjects and reply routing."""
    match = QUOTE_LABEL.match(candidate.name or "")
    return match.group(1) if match else candidate.deal_id


def work_summary(candidate: Candidate) -> str:
    """The middle of `Q-0412 website rebuild, Northside Dental`, used in body copy."""
    name = candidate.name or ""
    without_label = QUOTE_LABEL.sub("", name).strip()
    return without_label.split(",")[0].strip() or "the work we quoted"


def reply_to(client: Client, candidate: Candidate) -> str:
    """`quotes+<deal_id>@<reply_domain>`: the plus address is how a reply is matched
    back to one quote without guessing from the sender."""
    return f"quotes+{candidate.deal_id}@{client.reply_domain}"


def render(
    client: Client,
    candidate: Candidate,
    step: Step,
    opening_line: str,
    repo_root: Path | str = ".",
) -> str:
    body = (Path(repo_root) / step.template).read_text(encoding="utf-8")
    return body.format(
        opening_line=opening_line,
        quote_label=quote_label(candidate),
        work_summary=work_summary(candidate),
        currency=client.currency,
        amount=f"{candidate.amount:,.0f}",
        from_name=client.from_name,
        from_address=client.from_address,
        business_name=client.business_name,
    )


def build_message(
    client: Client,
    candidate: Candidate,
    step: Step,
    opening_line: str,
    now: datetime,
    message_id: str,
    repo_root: Path | str = ".",
) -> EmailMessage:
    message = EmailMessage()
    message["Message-ID"] = message_id
    message["From"] = f"{client.from_name} <{client.from_address}>"
    message["To"] = candidate.contact_email or "unknown@example.invalid"
    message["Reply-To"] = reply_to(client, candidate)
    message["Subject"] = step.subject.format(quote_label=quote_label(candidate))
    message["Date"] = now.strftime("%a, %d %b %Y %H:%M:%S %z")
    message.set_content(render(client, candidate, step, opening_line, repo_root))
    return message
