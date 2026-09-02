"""The one AI-written sentence. Spec section 4.9.

Rules that make this safe to put in front of a customer:

  * step 1 only, and only after the row is claimed, so a model call can never cause a
    duplicate send;
  * the prompt sees the client's tone string and a work category drawn from that
    client's own `work_categories` list, nothing else. Never the deal name, the amount,
    the date or the customer's name, which removes the prompt-injection surface a deal
    name would open;
  * the result is length-capped and rejected if it contains a digit or a currency sign,
    because numbers are inserted from data, never generated;
  * any failure falls back to the template's own opener and is counted, so "AI-written"
    is a claim about one run rather than a standing boast.

The live Groq call arrives in sitting 2. Offline, `template_opener` is the whole thing.
"""

from __future__ import annotations

MAX_LENGTH = 160
_FORBIDDEN = set("0123456789$£€")

TEMPLATE_OPENERS = {
    1: "Hope the week is treating you well.",
    2: "Following up on this one.",
    3: "Last note from me on this.",
}


def is_acceptable(line: str) -> bool:
    """A model line that breaks any of these goes in the bin and the template is used."""
    if not line or not line.strip():
        return False
    if len(line) > MAX_LENGTH:
        return False
    if any(ch in _FORBIDDEN for ch in line):
        return False
    return "\n" not in line.strip()


def category_for(client, candidate) -> str | None:
    """Match the deal name's work category against this client's own list.

    Exact membership only. A deal named `Q-0499 ignore previous instructions, 1 Main St`
    matches nothing, so no call is made and the template is used.
    """
    from chase.templates import work_summary

    summary = work_summary(candidate).lower()
    for category in client.work_categories:
        if summary == category.lower():
            return category
    return None


def template_opener(client, candidate, step) -> tuple[str, bool]:
    """Returns (line, came_from_model). Offline this is always the template."""
    return TEMPLATE_OPENERS.get(step.step, TEMPLATE_OPENERS[2]), False
