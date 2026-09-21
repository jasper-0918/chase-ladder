"""The demo board. Spec section 4.12.

A fixed set, so every demo starts from the same portal and a recording can be retaken
without the numbers moving. The board is computed by `demo_deals`, which touches nothing
and is therefore the part worth asserting; `seed` is the thin loop that writes it.

Two marks make the seed findable later: every deal name starts `Q-` and every contact
lives at `customer.example`. `reset` uses them to archive what it wrote and nothing else,
which matters because this runs against a real portal that may hold real deals.

Dates are relative to `clock.now()`, never to the wall clock, so a board seeded under
offset `0d` and a run under `4d` behave exactly like four real days passing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

CONTACT_DOMAIN = "customer.example"
DEAL_PREFIX = "Q-"

# Ages in days, chosen so a 3/7/14 ladder fires all three steps on the very first run
# and leaves a few deals already exhausted. A demo that only ever shows step 1 does not
# show the ladder at all.
LIVE_AGES = (1, 1, 2, 2, 4, 4, 5, 6, 8, 8, 9, 10, 12, 13, 15, 16, 18, 20, 22, 25)

# Stage, and how many days back the quote went out. Dressing exists so the board does not
# read as "everything is mid-chase", and so Won and Lost have something to stop.
DRESSING = (("Draft", 1), ("Won", 6), ("Won", 11), ("Lost", 9))

CUSTOMERS = (
    ("Marrickville Cycles", "Nadia", "Fenton"),
    ("Brunswick Bakehouse", "Oliver", "Marsh"),
    ("Coogee Dental Studio", "Priya", "Raman"),
    ("Thornbury Timber", "Daniel", "Okafor"),
    ("Fitzroy Physio", "Amelia", "Clark"),
    ("Glebe Glass", "Hugo", "Lindqvist"),
    ("Redfern Roasters", "Mei", "Tan"),
    ("Camperdown Electrical", "Jonah", "Whitfield"),
    ("Newtown Notary", "Saoirse", "Byrne"),
    ("Leichhardt Landscapes", "Marco", "Bellini"),
    ("Ashfield Auto", "Farida", "Haddad"),
    ("Balmain Blinds", "Toby", "Ashworth"),
    ("Enmore Upholstery", "Lena", "Novak"),
    ("Rozelle Roofing", "Kwame", "Boateng"),
    ("Summer Hill Signage", "Ines", "Delgado"),
    ("Petersham Plumbing", "Callum", "Reid"),
    ("Stanmore Stonework", "Yuki", "Nakamura"),
    ("Dulwich Hill Dairy", "Rosa", "Iversen"),
    ("Haberfield Hardware", "Emeka", "Nwosu"),
    ("Annandale Awnings", "Greta", "Sollberger"),
    ("Lilyfield Lighting", "Arjun", "Mehta"),
    ("Erskineville Espresso", "Clara", "Muller"),
    ("Tempe Tiling", "Sione", "Fifita"),
    ("Sydenham Stationery", "Beatrix", "Kovacs"),
)

# Deterministic and unremarkable: a demo board should not look like it was rolled.
AMOUNTS = (
    4200, 12800, 6350, 9900, 3150, 18400, 5600, 7450, 2900, 15200,
    8100, 4750, 11300, 6800, 3400, 9250, 5050, 13700, 7900, 2600,
    10400, 4400, 6150, 8850,
)


@dataclass
class SeedDeal:
    name: str
    amount: float
    stage: str
    quote_sent_at: datetime | None
    contact_email: str
    contact_first: str
    contact_last: str


@dataclass
class SeedReport:
    created: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        return f"seed: created {self.created}, failed {self.failed}"


def _midnight_utc(now: datetime, days_ago: int) -> datetime:
    """The day the quote went out, with the time dropped rather than rounded.

    A HubSpot Date property is refused unless it is midnight UTC, and rounding 23:30 up
    would silently move a quote into the next day and delay its whole ladder by one rung.
    """
    day = (now - timedelta(days=days_ago)).astimezone(now.tzinfo)
    return day.replace(hour=0, minute=0, second=0, microsecond=0)


def demo_deals(client, now: datetime) -> list[SeedDeal]:
    """The whole board, in a fixed order, without touching the network."""
    categories = client.work_categories
    deals: list[SeedDeal] = []
    plan = [("Sent", age) for age in LIVE_AGES] + list(DRESSING)

    for index, (stage, days_ago) in enumerate(plan):
        business, first, last = CUSTOMERS[index]
        category = categories[index % len(categories)]
        deals.append(
            SeedDeal(
                name=f"{DEAL_PREFIX}{400 + index:04d} {category}, {business}",
                amount=float(AMOUNTS[index]),
                stage=stage,
                quote_sent_at=_midnight_utc(now, days_ago),
                contact_email=f"{first.lower()}.{last.lower()}@{CONTACT_DOMAIN}",
                contact_first=first,
                contact_last=last,
            )
        )
    return deals


def seed(hubspot, client, now: datetime) -> SeedReport:
    """Write the board. One deal that will not write does not stop the rest.

    A half-seeded portal is easy to see and easy to reset, while an exception halfway
    through leaves no report saying how far it got.
    """
    report = SeedReport()
    for deal in demo_deals(client, now):
        try:
            contact_id = hubspot.create_contact(
                deal.contact_email, deal.contact_first, deal.contact_last
            )
            deal_id = hubspot.create_deal(
                name=deal.name,
                amount=deal.amount,
                stage=deal.stage,
                quote_sent_at=deal.quote_sent_at,
            )
            hubspot.associate_contact(deal_id, contact_id)
            report.created += 1
        except Exception as exc:  # noqa: BLE001
            report.failed += 1
            report.errors.append(f"{deal.name}: {exc}")
    return report
