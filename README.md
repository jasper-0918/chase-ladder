# Chase Ladder

Chase Ladder chases quotes a small business sent and never heard back on. It is designed to read
open deals from HubSpot Free and email each customer at set steps, such as 3, 7 and 14 days after
the quote. The design stops the chase when the customer replies or the deal is won or lost.

## Status, 2026-09-21

`run` works. It reads open deals from HubSpot, polls Mailpit for replies, and sends over SMTP.

Built and tested on `main`, with 226 tests passing:

- the clock, YAML loader and ladder arithmetic
- the send window and SQLite send log
- the templates, run report and `run_ladder`
- `HubSpotClient`, over a fake transport
- `SmtpSender`, including which SMTP failures may be retried and which may never be
- the Mailpit reply poll and its plus-address parse
- the CLI, including the `.env` loader

`status`, `reset`, `run` and `run --dry` all work. Against a live portal with Mailpit up,
`run --dry` completes and reports.

**No chase has actually been sent yet**, because seeding is still unbuilt and the demo portal
holds no deals in Sent or Chasing. One deal created by hand, with `quote_sent_at` a few days back
and a contact attached, is enough to watch a chase land in Mailpit.

Designed, not built:

- HubSpot seeding, `doctor`, Docker Compose and the demo video
- the HTTP routes and the Friday digest over Telegram
- n8n scheduling and the Groq opening line

Known gaps:

- No test fails if `db.claim` or `db.record_stop` uses a plain `BEGIN`.
- `run_ladder` reads and claims one due deal at a time, where the spec reads them all first. The
  run no longer loses its report when a read fails partway, but the read pattern still differs
  from the spec.
- `run._send_one` stamps each claim with the run's `now`, not the clock at claim time. In a run
  longer than the grace window, five minutes by default, a late claim can look unresolved to
  `status` or an overlapping run while it is still sending.
- `send_window.tz` is not checked at load, so a misspelt zone fails after the reply poll.
- `run_ladder` moves a Sent or Chasing deal to Replied for any earlier reply stop. The spec limits
  it to this run's.

Three gaps closed on 2026-09-21, each with tests that reproduce the failure first: a deal closed
by the customer mid-send is no longer dragged back and chased again; a raise between the claim and
the send is now a retryable failure rather than a stranded row and a lost run; and a HubSpot read
that fails after a send ends the run with its report instead of throwing it away.

## What it claims

Section 1 of the design spec, written before any schema, caps what this README and any demo may
claim for the finished system.

> Every quote a business sends is chased on a schedule the business sets in one YAML file, and the
> chasing stops when the customer replies, when the deal is moved to Won or Lost, or when someone
> tells it to stop. Each quote and step gets **at most one successful send**; a send SMTP refused is
> marked failed and retried on a later run. Re-running the ladder adds zero rows. A crash between
> the ledger commit and the email send is reported as *unresolved*, with a count, and is never
> retried automatically. There is **no lost update between a stop and a claim**, because the run and
> the stop both write the same SQLite file under `BEGIN IMMEDIATE`; the Won/Lost path is a direct
> HubSpot read of each due deal before any claim, once per run, with an inherent window this
> [design] document names. A Friday digest says **which quotes are still unanswered and how much is
> sitting in them**.

Read it literally. The email leaves over SMTP, outside the SQLite transaction. The named window:
a reply after the reply poll, or a move to Won or Lost after the direct read, can still get that
run's email. The next run's poll stops a late reply. A late move to Won or Lost is caught by the
next run's direct read, unless the deal was in Sent: the send's PATCH moves it back to Chasing.

Tests on `main` already back these parts:

- at most one successful send per quote and step
- a refused send retried until a later step comes due
- zero new rows on a re-run
- an ambiguous send left `claimed`
- a fresh HubSpot read of each due deal before its claim

## Notes on the code

**The claim transaction.** `db.claim` opens `BEGIN IMMEDIATE` and holds the write lock before
reading `stops`. Its insert ends in `ON CONFLICT (deal_id, step) DO UPDATE ... WHERE
reminder_log.status = 'failed'`, so only a refused step can be claimed again. `tests/test_claim.py`
runs a claim and a stop on two threads and checks the end state. Its lock test issues
`BEGIN IMMEDIATE` by hand, not through `db.claim`.

**Refused versus ambiguous sends.** `run._send_one` marks a `RefusedBeforeDelivery` as `failed`.
Any other exception from the send, a timeout included, leaves the row `claimed`, since the server
may already hold the message. `tests/test_run.py` tests this with a fake mailer and a stand-in for
`db.claim`. Nothing tests the smtplib errors listed in `mailer._REFUSALS`.

**Stage labels resolved to ids.** The run uses stage labels and HubSpot's `dealstage` holds ids, so
`HubSpotClient` maps them from one pipeline read. `tests/test_hubspot_integration.py` runs
`run_ladder` through the real client, `db.claim` and `in_send_window` over a fake transport. Two of
four deals are sent, one is stopped as Won by its direct read, and the first-step PATCH moves its
deal by stage id, not label. It has no failed send.

**DST tests with real dates.** `in_send_window` converts to the client's IANA zone first.
`tests/test_window.py` requires 2026-10-03T16:00Z to fall inside a Sunday 03:00 to 04:00 Sydney
window, where a fixed +10 hours gives 02:00. Both 01:30s of 2026-11-01 in Chicago must fall inside
01:00 to 02:00.

**One clock.** Only `chase/clock.py` in the package calls `datetime.now(`, and
`CHASE_CLOCK_OFFSET` shifts it for demos. `tests/test_no_datetime_now.py` checks by text search, so
an aliased import slips past it.

## Demo data

Harbourline Digital (Sydney) and Lakeshore Fitout (Chicago) are fictional. Every email address in
tracked files is under the reserved `.example` top-level domain, plus one `unknown@example.invalid`
fallback. Lakeshore is never seeded into HubSpot and is exercised only by tests: HubSpot Free has
one pipeline, the design adds no field to split businesses, and `search_candidates` ignores its
client argument.

## Running the tests

Needs Python 3.12 or newer, because `db.connect` passes `autocommit=True`.

```text
python -m pip install -r requirements.txt
python -m pytest -q
python -m chase status
```

Tests need no credentials or network and ran on Python 3.14.6. Leave `CHASE_CLIENT` and
`CHASE_UNRESOLVED_AFTER` unset: `tests/test_cli.py` expects the default client, and a blank grace
window fails five tests. `status` writes the gitignored `data/chase.db`. `fastapi` and `uvicorn`
are for the unbuilt HTTP routes.

`.env.example` names the variables the finished system uses, but nothing reads `.env`. The code
reads only `CHASE_CLIENT`, `CHASE_CLOCK_OFFSET` and `CHASE_UNRESOLVED_AFTER`, from the shell. Unset,
each takes a default. A blank offset stops `status` with a `ValueError`.

## Repository layout

```text
chase/clock.py      the demo clock and its offset
chase/config.py     loads and checks config/<client>.yaml
chase/ladder.py     which step is due
chase/window.py     the send window in the client's zone
chase/db.py         schema, claim, mark, record_stop
chase/run.py        run_ladder, one pass
chase/hubspot.py    HubSpotClient
chase/mailer.py     Message-IDs, SmtpSender and the refusal split
chase/templates.py  builds each email
chase/opener.py     template opening lines and guards for a model-written one
chase/report.py     RunReport counters
chase/cli.py        python -m chase with status, run and reset
config/             the two client YAML files
templates/          seven step templates
tests/              pytest suite with hand-written fakes, no network
docs/               the design spec
```

## Design

The [design spec](docs/superpowers/specs/2026-08-29-chase-ladder-design.md) describes the finished
system, not the code on `main`.
