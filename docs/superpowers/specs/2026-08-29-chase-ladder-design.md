# Chase Ladder, Design

Date: 2026-08-29. Status: settled after the three-critic audit of 2026-08-29, pending Jasper's read.
Cost: $0 throughout. No card is entered anywhere, no trial clock starts anywhere.
Build: solo, four sittings of 2 to 3 hours. Three build, the fourth writes the Loom script and
records. The fourth exists because the script is real work and had no slot in a three-evening plan.
Shelf life: the Loom is re-recorded within six months of the first recording, so vendor changes
inside that window are runbook lines, not risks.

## 1. Goal

**Claims.** This paragraph was written before any schema and is the ceiling on what the README and the
Loom may use.

> Every quote a business sends is chased on a schedule the business sets in one YAML file, and the
> chasing stops when the customer replies, when the deal is moved to Won or Lost, or when someone
> tells it to stop. Each quote and step gets **at most one successful send**; a send SMTP refused is
> marked failed and retried on a later run. Re-running the ladder adds
> zero rows. A crash between the ledger commit and the email send is reported as *unresolved*, with a
> count, and is never retried automatically. There is **no lost update between a stop and a claim**,
> because the run and the stop both write the same SQLite file under `BEGIN IMMEDIATE`; the Won/Lost
> path is a direct HubSpot read of each due deal before any claim, once per run, with an inherent
> window this document names. A
> Friday digest says **which quotes are still unanswered and how much is sitting in them**.

A small business sends quotes and then forgets to follow them up. Chase Ladder watches the deals in
the business's HubSpot Free portal, sends a follow-up email at each step of a ladder (for example 3,
7 and 14 days after the quote went out), inside the business's own sending hours, and stops the
moment a customer replies or the owner moves the deal to Won or Lost. Every decision lives in Python
and is tested; n8n is the scheduler and the notifier; SQLite is the send log; HubSpot is the only
place quotes live.

Both demo businesses quote through a CRM on purpose. Australian trades can already buy automated
quote chasing inside the field-service app they pay for, from AUD 29 a month,
verified(https://www.servicem8.com/au/pricing), and those apps are not HubSpot, so a tradie watching
the Loom would rightly ask why not the button in the app he already has. A Sydney web and brand
agency and a Chicago commercial interiors contractor both quote out of a CRM and have no vertical
tool that does this for them, so the demo is answering a question they actually carry.

Two audiences, one build:

| Audience | What must land |
| --- | --- |
| A small-business owner watching a 2 to 3 minute Loom | Six on-camera beats (section 13): the unanswered quotes before, the ladder chasing, a re-run refusing to double-send, a customer reply stopping it, the owner closing a deal and the chasing standing down, and the Friday digest on the phone, closing with the run shouting when the mail server is unreachable |
| A hiring engineer reading the repo | One transaction they can reason about, a run report that counts its own failures, a two-column seeded-versus-measured table, and vendor limits that carry a URL or the word unverified |

## 2. Non-goals (YAGNI)

Everything below was considered and cut on 2026-08-29. None of it is a later phase of this build;
some of it is a deferred option (section 15).

- No Postgres. SQLite in WAL mode serialises the two writers (the run and the stop) through one
  file lock on one local disk, deliberately. Postgres would add a container and an install risk
  for no story.
- No quotes mirror table in SQLite. HubSpot is the only quote store. Two stores for the same quotes
  means the seed writes twice and a stranger gets two answers to "where is this customer's email".
- No invoice ladder, no payments webhook, no ledger or mark-paid UI, no Invoice Ninja. A quote is
  accepted, not paid. HubSpot Free permits one deal pipeline, so invoices had nowhere to live, and
  the pitch is not allowed to promise accounts receivable.
- No status page. HubSpot's board, Mailpit's inbox, Telegram and `python -m chase status` are the
  four surfaces; a fifth would go stale by the second week.
- No sender-email fallback in reply matching. It never fires in a Mailpit demo, so it would ship
  untested, and it is wrong for a customer with two open quotes. An `unmatched_replies` counter
  surfaces the case instead.
- No `chase_state`, `chase_step` or `deal_type` HubSpot properties. Stage carries state, the send log
  carries the step, and the second client is tests-only, so the third has no job.
- No `SIM_CLOCK` multiplier. Tests would depend on wall-clock time and the re-run beat on how long
  Jasper talked. The clock is an offset, not a speed.
- No n8n hop on the reply path. Mailpit posts straight to Python. Every extra hop on a webhook that
  never retries is strictly more loss.
- No Python-side Telegram. Python returns JSON and text; only n8n holds the bot token.
- No second client live in the portal. HubSpot Free has one pipeline and, after the property cut, no
  discriminator; the Lakeshore client is exercised by tests and a README diff.
- No AI beyond one bounded opening line on the first chase. Steps 2 and 3 are "still keen?"
  templates where a model getting creative is a liability.

## 3. Architecture

```
                         python -m chase run | digest | status | reset | seed | demo-reply | doctor
                                            |
                                            v
  +---------------------+     +---------------------------------------------+     +-------------------+
  |  HubSpot Free       |     |  Python 3.14, FastAPI + CLI, one worker     |     |  SQLite (WAL)     |
  |  (demo portal)      |<--->|  package `chase`                            |<--->|  data/chase.db    |
  |  deals = quotes     | API |  /run-ladder  /digest  /stop  /hooks/mailpit|     |  reminder_log     |
  |  stages, 2 props    |     |  shared-secret header on all four           |     |  stops            |
  +---------------------+     |  clock.py = the only datetime.now()         |     +-------------------+
        ^                     +------+-------------------+------------------+
        |  stop (2): direct read            |                   |
        |  of Won/Lost before any claim     | SMTP 1025         | one call, step 1 only
        |                                   v                   v
        |                     +----------------------+    +----------------------+
        |                     |  Mailpit (Docker)    |    |  Groq                |
        |                     |  catches every mail  |    |  openai/gpt-oss-120b |
        |                     |  /api/v1/search poll |    |  strict JSON schema  |
        |                     |  webhook -> Python   |    |  3 s timeout         |
        |                     +----------+-----------+    +----------------------+
        |                                |
        |   stop (1): reply, poll at run start (correctness) + webhook (fast path)
        |
        |   stop (3): manual, POST /stop {reason: reply|stage|manual}
        |
  +-----+---------------------------------------------------------------------+
  |  n8n (his existing instance, optional wrapper), one workflow              |
  |  Schedule -> HTTP POST host.docker.internal:8000/run-ladder               |
  |           -> IF status != ok ----------------> Telegram alert             |
  |           -> HTTP error output --------------> the same alert node        |
  |  Friday Schedule -> HTTP GET /digest -> Telegram digest                   |
  +----------------------------------+----------------------------------------+
                                     |
                                     v
                            +------------------+
                            |  Telegram bot    |
                            |  owner's phone   |
                            +------------------+
```

Who decides what:

| Concern | Lives in | Never in |
| --- | --- | --- |
| Which quotes exist, their amount, contact, stage, `quote_sent_at` | HubSpot | SQLite |
| Whether a (deal, step) chase has been claimed, sent or failed | SQLite `reminder_log` | HubSpot |
| Whether a deal is stopped and why | SQLite `stops` | HubSpot (stage is an input, not the record) |
| Due maths, send window, claim, send, counters | Python | n8n |
| When to run, who to alert, the Friday digest delivery | n8n | Python |
| The Telegram bot token | n8n credential | Python, `.env`, the repo |
| The HubSpot App Token and the Groq key | local `.env` | chat, the repo, n8n |

## 4. Components

### 4.1 Python package and CLI

Package `chase`, Python 3.14 on the Windows host (not in Docker, so SQLite's WAL shared memory
stays on one local filesystem). FastAPI served by uvicorn with exactly one worker. Sync endpoints, so
FastAPI runs them in a threadpool; the CLI run is a second process beside uvicorn. Either way every
writer takes the same file lock. uvicorn binds `0.0.0.0` on `CHASE_PORT` (default 8000) so the
containers can reach it, which
is the reason the shared secret exists (section 4.1.1); `/run-ladder` and `/digest` run the client
named by `CHASE_CLIENT` (default `harbourline`). `data/chase.db` is never bind-mounted into a
container.

Routes (all require the shared-secret header, section 4.1.1):

| Route | Method | Called by | Returns |
| --- | --- | --- | --- |
| `/run-ladder` | POST | n8n schedule (sends `{}`), `python -m chase run` (in-process, not over HTTP) | run report JSON (section 6); HTTP 500 with `{"status": "failed", "error": "<message>"}` when the run aborts |
| `/digest` | GET | n8n Friday schedule, `python -m chase digest` | `{text, unanswered_count, unanswered_total, currency, counters}` (section 4.15) |
| `/stop` | POST | anyone with the secret; README shows a two-line curl | `{deal_id, reason, ref, recorded}` with `recorded` `true` or `false` (`false` when the (deal, ref) pair already existed) |
| `/hooks/mailpit` | POST | Mailpit's webhook | `401` on a missing or wrong secret (query or header form); `202` for everything else, so an authenticated Mailpit never sees a failure it would not retry anyway |

Commands, `python -m chase <command>`:

| Command | Does |
| --- | --- |
| `run [--client harbourline] [--offset 4d]` | one ladder pass, prints the run report JSON, exit code 1 when `status` is `failed` |
| `digest` | prints the Friday digest text (section 4.15) |
| `status` | prints `reminder_log: N rows, stops: M rows` as its first line, then the table of `reminder_log` and `stops` joined to the candidate deals, then the counters from a dry pass; the count line is the "0 new `reminder_log` rows" surface on camera (a dry pass may add stop rows) |
| `reset` | wipes `reminder_log` and `stops`, deletes Mailpit's messages, archives the demo deals and contacts in HubSpot, re-seeds relative to the clock |
| `seed` | creates the 24 demo contacts and deals (section 4.12), a fixed set |
| `demo-reply <quote-label>` | sends a customer reply through `smtplib` to Mailpit on `CHASE_SMTP_PORT` (default 1025), addressed to the deal's plus address; said on camera as "I am playing the customer" |
| `doctor` | pre-flight checks (section 4.13), exit code 1 on any miss |

The CLI calls the same functions the routes call; there is one `run_ladder(client, now, dry=False)`
and one `build_digest(client, now)`. `dry=True` runs steps 1 to 4, 6 and 10 of section 4.5 (reply
stops from the poll are still written) and skips steps 5, 7, 8 and 9, so it writes nothing to
`reminder_log` and nothing to HubSpot; `status` and `digest` use it.

#### 4.1.1 Shared-secret header

`X-Chase-Secret` compared with `hmac.compare_digest` against `CHASE_SHARED_SECRET` from `.env`.
Missing or wrong: `401`, nothing read, nothing written. Four lines of code; without them any process
on the LAN can stop every ladder. n8n sends the header from its HTTP node's header auth; Mailpit
sends it because `MP_WEBHOOK_URL` carries it as a query parameter that the route also accepts
(whether Mailpit's webhook can send a custom header is unverified, so the query form is the
documented Mailpit path and the header form is what everything else uses).

### 4.2 clock

`chase/clock.py` is the only module that calls `datetime.now()`. A test greps the package and fails
on any other call site.

```python
# chase/clock.py
def offset() -> timedelta:        # read on every call, so a flag set before the run is honoured
    return parse_offset(os.environ.get("CHASE_CLOCK_OFFSET", "0d"))   # "4d", "4d6h", "-2h"

def now() -> datetime:            # aware, UTC
    return datetime.now(timezone.utc) + offset()

def now_local(tz: str) -> datetime:
    return now().astimezone(ZoneInfo(tz))
```

The CLI flag `--offset` sets `os.environ["CHASE_CLOCK_OFFSET"]` before calling `run_ladder`, so it
overrides the environment for that invocation. The FastAPI process reads `CHASE_CLOCK_OFFSET` from
its own environment and there is no per-request override; moving the server's clock means
restarting uvicorn with the new value. The offset feeds due maths,
the send window, `reminder_log.claimed_at` and `sent_at`, `stops.stopped_at`, and the
`last_chase_at` written to HubSpot, all alike. The run's `now` is read once at the top of the run,
for due maths, the window and `last_chase_at`; `claimed_at`, `sent_at` and `stopped_at` are read
from `clock.now()` when the row is written, so a row is stamped when it happened rather than when
its run began (section 4.5 step 8 says why that matters). Seed dates are computed from `clock.now()`, so a
seed created under offset `0d` and a run under `4d` behave exactly like four real days passing.
`tzdata` is declared in `requirements.txt`, because `ZoneInfo` has no timezone database on Windows
without it, verified(https://docs.python.org/3/library/zoneinfo.html).

On camera the sentence is: "I am fast-forwarding the calendar, nothing else is faked."

### 4.3 Client YAML

One file per client under `config/`. One loader, `load_client(name) -> Client`, with schema
validation so a missing key fails at start, not mid-run. The two files must differ in policy, not
only in three integers.

`config/harbourline.yaml` (the live demo client, a six-person web and brand agency in Sydney whose
quotes are project proposals; seeded into the portal):

```yaml
client: harbourline
business_name: Harbourline Digital
city: Sydney
currency: AUD
from_name: Harbourline Digital
from_address: quotes@harbourline.example
reply_domain: harbourline.example        # Reply-To becomes quotes+<deal_id>@harbourline.example
tone: friendly, plain, one short question at the end
work_categories:                           # the only values that may reach the model, section 4.9
  [brand identity, website rebuild, SEO retainer, ecommerce build, content strategy, design system]

ladder:
  - step: 1
    days_after_sent: 3
    subject: "Quote {quote_label}: any questions?"
    template: templates/harbourline/step1.txt
    opener: groq                            # the only AI-written sentence in the system
  - step: 2
    days_after_sent: 7
    subject: "Still keen on {quote_label}?"
    template: templates/harbourline/step2.txt
    opener: template
  - step: 3
    days_after_sent: 14
    subject: "Last check on {quote_label}"
    template: templates/harbourline/step3.txt
    opener: template

send_window:
  days: [mon, tue, wed, thu, fri]
  start: "08:00"
  end: "18:00"
  tz: Australia/Sydney

stop_on:
  stages: [Won, Lost]                       # checked in the direct read of each due deal before a claim

digest:
  day: fri
  time: "16:00"                             # in send_window.tz; push_n8n.py copies day and time into the n8n schedule
  stale_after_days: 7                       # "untouched for 7+ days" threshold
```

`config/lakeshore.yaml` (the second client, a commercial interiors contractor in Chicago; commercial
fit-out work, not residential trade, so no vertical tool already does this for them; tests and
README diff only, never seeded):

```yaml
client: lakeshore
business_name: Lakeshore Fitout
city: Chicago
currency: USD
from_name: Lakeshore Fitout
from_address: quotes@lakeshorefitout.example
reply_domain: lakeshorefitout.example
tone: direct, short, no small talk
work_categories:
  [office fitout, retail fitout, joinery package, partition works, ceiling works, site make-good]

ladder:
  - step: 1
    days_after_sent: 2
    subject: "Your fit-out quote {quote_label}"
    template: templates/lakeshore/step1.txt
    opener: groq
  - step: 2
    days_after_sent: 5
    subject: "Quote {quote_label}, still open"
    template: templates/lakeshore/step2.txt
    opener: template
  - step: 3
    days_after_sent: 10
    subject: "Closing out quote {quote_label}"
    template: templates/lakeshore/step3.txt
    opener: template
  - step: 4
    days_after_sent: 20
    subject: "Closing quote {quote_label}"
    template: templates/lakeshore/step4.txt
    opener: template

send_window:
  days: [mon, tue, wed, thu, fri, sat]      # site handovers run on Saturdays here
  start: "09:00"
  end: "17:00"
  tz: America/Chicago

stop_on:
  stages: [Won, Lost]

digest:
  day: fri
  time: "15:00"
  stale_after_days: 5
```

The policy differences a reader should be able to spot in the README diff: ladder 3/7/14 versus
2/5/10/20, ladder length three versus four steps, five-day versus six-day window in two DST
regimes, tone string, and the digest's stale threshold. `stop_on.stages` is the same in both on
purpose: a deal in Replied is not a candidate for either client, so listing it there would be a
policy difference nobody could observe.

**Sender identification and unsubscribe.** The Australian Spam Act 2003 requires consent, requires
a commercial electronic message to identify the business that authorised it by its correct legal
name (or its name and ABN), and requires an unsubscribe option that does not ask for extra personal
details or an account, verified(https://www.acma.gov.au/avoid-sending-spam). Every chase template
therefore ends with the sending business's name and `from_address` and with one unsubscribe line:
reply with the word STOP and this quote is never chased again. Two honest limits go in the README
beside it. That STOP reply is carried by the same plus-address path as any other reply, so a
customer who strips `Reply-To` and answers the `From` address is not detected, exactly as the
failure table says. And a fictional business cannot supply a legal identity or an ABN, so a real
deployment supplies both. No deadline for actioning an unsubscribe is asserted here: ACMA's page
states the requirement without one, and this document does not repeat numbers it has not read.

`in_send_window(now_utc, send_window) -> bool` is a pure function: convert to `send_window.tz`,
check the weekday against `days`, check `start <= local time < end`. Tests feed UTC instants around
2026-10-04 (Sydney springs forward 02:00 to 03:00, verified against the `tzdata` database at test
time) and 2026-11-01 (Chicago falls back 02:00 to 01:00), both Sundays: a Sunday-only test window
covers the repeated and the missing hour, and the shipped windows are checked on the Friday before
and the Monday after each change so the one-hour shift is visible.

### 4.4 SQLite schema

`data/chase.db`, gitignored together with its `-wal` and `-shm` siblings. Created on start by
`db.init()`. Every connection is opened per request or per CLI invocation, with
`sqlite3.connect(path, timeout=5.0, autocommit=True)` so that every transaction is opened by hand
with `BEGIN IMMEDIATE`; Python's default `DEFERRED` transactions turn the read-then-write in the claim
into `SQLITE_BUSY_SNAPSHOT`, which the busy timeout does not rescue,
verified(https://www.sqlite.org/rescode.html), verified(https://www.sqlite.org/isolation.html).

```sql
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS reminder_log (
    deal_id     TEXT    NOT NULL,                      -- immutable HubSpot deal id, never the name
    step        INTEGER NOT NULL,
    status      TEXT    NOT NULL CHECK (status IN ('claimed', 'sent', 'failed')),
    claimed_at  TEXT    NOT NULL,                      -- ISO 8601 UTC, clock.now() read inside the claim
    sent_at     TEXT,
    message_id  TEXT    NOT NULL,                      -- generated before the send, written by the claim
    UNIQUE (deal_id, step)
);

CREATE TABLE IF NOT EXISTS stops (
    deal_id     TEXT    NOT NULL,
    reason      TEXT    NOT NULL CHECK (reason IN ('reply', 'stage', 'manual')),
    ref         TEXT    NOT NULL,                      -- reply: the reply's Message-ID; stage: 'stage:won';
                                                       -- manual: caller-supplied or 'manual:<stopped_at>'
    stopped_at  TEXT    NOT NULL,
    UNIQUE (deal_id, ref)
);
```

Two tables, nothing else. `UNIQUE (deal_id, step)` is the at-most-once artefact: it holds one row per
(deal, step) forever, and that row's status says whether the send succeeded. `UNIQUE (deal_id,
ref)` is the Message-ID dedupe: the webhook and the run-start poll can both see the same reply and
only one row results. A deal can carry several stop rows (a reply and then a stage move); any row
stops it.

Row states in `reminder_log`:

| status | Meaning | Who sets it | Blocks the step? |
| --- | --- | --- | --- |
| `claimed` | the ladder committed its intent to send this step; the SMTP send may or may not have happened | the claim transaction | yes, permanently, until a human resolves it |
| `sent` | Mailpit accepted the message; `sent_at` filled | the mark step after SMTP returned | yes, permanently |
| `failed` | the SMTP server refused the message before accepting it (connection refused, bad greeting, sender or recipient rejected), so nothing was accepted | the mark step, on those errors only | no; the next run may take the row over and try again (section 4.5 step 8) |

An SMTP call that raises anything else, a timeout or a connection reset, is **not** marked `failed`,
because the server may already hold the message. The row stays `claimed`, which no run ever retries,
and it surfaces as `unresolved` for a human to settle from Mailpit. Retrying an ambiguous send is
the one way this design could deliver the same chase twice, so it does not.

`message_id` is filled on every row, whatever the status, because it is generated before the send
and written by the claim itself. That is what makes a stuck row findable: a `claimed` row always
carries the Message-ID its message would have used, so Mailpit can be searched for it.

A row that is still `claimed` and whose `claimed_at` is more than `unresolved_after` (default 5
minutes, `CHASE_UNRESOLVED_AFTER`) before now is
`unresolved`. It is reported, never resent, and
the runbook line for clearing it is a human decision: search Mailpit for the row's `message_id`,
then set the row to `sent` or
`failed` by hand with the one-line SQL in the README. There is no `resolve` command, on purpose.

### 4.5 The ladder run, step by step

`run_ladder(client, now, dry=False)`:

1. **Load** the client YAML; the caller passes `now = clock.now()`.
2. **Candidates.** HubSpot CRM Search for deals whose `dealstage` is in {id(Sent), id(Chasing)}
   (labels are mapped to stage ids on load, section 4.7), requesting
   `dealname, amount, dealstage, quote_sent_at, last_chase_at`. One page of 200 covers the demo.
   `candidates` = the count. A search that fails after its one retry aborts the run (section 6).
3. **Mailpit reconciliation poll.** `GET /api/v1/search?query=to:quotes+` and, for each message,
   parse `quotes+<deal_id>` from the local part of `To[].Address`. A plus address matches when its
   deal id appears in this run's candidate list or in `reminder_log`; on a match,
   `record_stop(deal_id, 'reply', MessageID)` (a match outside the candidate list still records
   the stop, harmlessly, and is not counted as unmatched); no match, `unmatched_replies += 1`. This
   is the correctness path; the webhook (section 4.6) is only latency. A poll failure aborts the
   run: chasing without the correctness path is the one thing this design refuses to do.
4. **Due step per candidate**, from the search data and `reminder_log` (`quote_sent_at` is in the
   search result, so no per-candidate read happens here). `due_step(candidate, ladder, now, log_rows)`:
   - eligible steps are those whose `quote_sent_at + days_after_sent <= now` **and** whose number is
     greater than the highest step already in `reminder_log` for the deal **with status `claimed` or
     `sent`**; a `failed` row does not block its own step, so a step SMTP refused is eligible again
     on the next run;
   - the highest eligible step is the one to send; lower unsent steps are skipped for good, so a
     quote that is 10 days old on the first run gets step 2 once, not step 1 and step 2 in one
     minute. A failed step is therefore retried only while no higher step has come due; once one has,
     the ladder moves on, exactly as it does for a step whose day passed during an outage;
   - `ladder_exhausted` = candidates whose last ladder step (step 3 for Harbourline) has a
     `claimed` or `sent` `reminder_log` row and which have no stop row. A `failed` last step is not
     exhaustion, it is a retry waiting for the next run.
   `due` = candidates with an eligible step now, stop rows ignored; `stopped` is the subset refused
   at claim time.
5. **Projection repair.** Any candidate whose reply stop row was recorded **during this run** (by
   step 3's poll) is PATCHed to `dealstage = Replied`; a failure counts in `hubspot_sync_failed`.
   "During this run" means `record_stop` returned true here, so the set is exactly the replies this
   run's poll discovered. It is deliberately narrow: a reply stop recorded on an earlier run is never
   re-PATCHed, so a deal the owner has since dragged somewhere else is never silently dragged back.
   The price is that a PATCH lost by the webhook stays lost, and the deal sits on the board in
   Chasing while the ladder has in fact stopped; every claim for it is refused and counted in
   `stopped`, and the projection note in section 5 is the sentence that covers it. Skipped when
   `dry`.
6. **Send window.** `in_send_window(now, client.send_window)` false: every due deal counts as
   `skipped_window`, no `reminder_log` row is written (stop rows from step 3 stand), the run ends
   `ok`.
7. **Direct read of every due deal**, as a batch before any claim (skipped when `dry`):
   `GET /crm/v3/objects/deals/{id}?associations=contacts`, because search results lag updates by
   "a few moments", verified(https://developers.hubspot.com/docs/api-reference/latest/crm/search-the-crm).
   One call returns the fresh stage and the contact id; one contact read returns the email;
   sequential, with a `Retry-After` sleep on 429. A due deal whose fresh stage is in
   `stop_on.stages` (Won or Lost) gets `record_stop(deal_id, 'stage', 'stage:' + name.lower())` and
   its claim is then refused (`stopped`). A due deal whose fresh stage is anything else outside
   {Sent, Chasing} (Replied, Draft) is dropped from this run with no row and no PATCH. Stop rows are
   never deleted and no code path deletes one, so a stop is final for that quote whatever the owner
   does to the stage afterwards; a deal parked in Replied by hand with no stop row behind it is a
   candidate again once it is dragged back to Chasing (section 5).
8. **Claim-then-send**, one deal at a time, serialised (this is also what keeps Groq under its
   per-minute limit; skipped when `dry`):

```python
def claim(conn, deal_id, step, message_id) -> Claim:
    conn.execute("BEGIN IMMEDIATE")                       # take the write lock first; waits up to busy_timeout
    try:
        if conn.execute("SELECT 1 FROM stops WHERE deal_id = ?", (deal_id,)).fetchone():
            conn.execute("ROLLBACK")
            return Claim.STOPPED                          # counted in `stopped`
        cur = conn.execute(
            "INSERT INTO reminder_log (deal_id, step, status, claimed_at, message_id) "
            "VALUES (?, ?, 'claimed', ?, ?) "
            "ON CONFLICT (deal_id, step) DO UPDATE SET "
            "    status = 'claimed', claimed_at = excluded.claimed_at, "
            "    sent_at = NULL, message_id = excluded.message_id "
            "  WHERE reminder_log.status = 'failed'",
            (deal_id, step, clock.now().isoformat(), message_id))   # wall clock, read here, not the run's now
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            return Claim.ALREADY_LOGGED                   # a 'claimed' or 'sent' row already holds this step
        conn.execute("COMMIT")
        return Claim.CLAIMED
    except Exception:
        conn.execute("ROLLBACK")
        raise

def send_step(conn, client, deal, step, now):
    message_id = make_message_id(client)                  # generated first, so a stuck row is findable
    outcome = claim(conn, deal.id, step, message_id)
    if outcome is not Claim.CLAIMED:
        return outcome
    # --- from here the row exists and only this thread will act on it ---
    opener = opening_line(client, deal, step)            # Groq on step 1 only, template otherwise, never raises
    msg = build_message(client, deal, step, opener, now, message_id)  # amounts, dates, names inserted here, after any model call
    try:
        smtp_send(msg, timeout=10)                        # smtplib to Mailpit on CHASE_SMTP_PORT, outside any
                                                          # transaction; the explicit timeout is what makes
                                                          # `unresolved_after` (5 min) a real margin
    except REFUSED_BEFORE_DELIVERY as exc:                # connect refused, greeting failed, sender or
        mark(conn, deal.id, step, status="failed")        # recipient rejected: the server took nothing
        return Claim.FAILED                               # BEGIN IMMEDIATE; UPDATE; COMMIT
    except Exception:                                     # timeout, reset, anything ambiguous: the server
        return Claim.UNKNOWN                              # may already hold the message, so the row STAYS
                                                          # `claimed` and a human decides
    mark(conn, deal.id, step, status="sent")              # sets sent_at from clock.now()
    project_to_hubspot(deal.id, last_chase_at=now,
                       dealstage="Chasing" if deal.fresh_stage == "Sent" else None)   # failure counted, never raised
    return Claim.SENT
```

   Three details in that block are load-bearing. `claimed_at` is `clock.now()` read **inside** the
   claim, not the run's fixed `now`: with the run's `now`, a second run starting while the first is
   still working would see the first run's in-flight rows as claimed before its own start and page
   the owner about a run that is going fine, and section 7 explicitly contemplates that overlap (an
   n8n retry beside a manual `run`).
   The `ON CONFLICT` clause takes over a `failed` row for the same (deal, step) and does nothing at
   all to a `claimed` or a `sent` one: a rung the server refused outright is retryable, a rung
   delivered or of unknown outcome is not, which is why the ambiguous SMTP error deliberately leaves
   the row `claimed` rather than marking it `failed`. And the SMTP send sits outside the transaction on
   purpose: a socket call inside a write lock would
   hold every stop for the length of a network timeout. The price is the window between `COMMIT`
   and `mark`, and that price is paid honestly as `unresolved`.

9. **Projection to HubSpot.** After `sent`, PATCH `last_chase_at = now`, and `dealstage = Chasing`
   only when the fresh read in step 7 was Sent. Any failure increments `hubspot_sync_failed` and
   does not change the SQLite row: SQLite is the truth, the two HubSpot properties are a projection
   that can lag or fail.
10. **Report.** Build the run report (section 6). `unresolved` = rows still `claimed` whose
    `claimed_at`, the wall clock at the moment of that claim, is more than `unresolved_after`
    (default 5 minutes, comfortably longer than the SMTP timeout) before now. A run's start time is
    the wrong threshold: a row claimed one second before this run began and still inside its SMTP
    call would be counted and would page the owner about a run that is going fine. The grace window
    is the honest fix, and its cost is that a genuine crash is reported one run later.
    `status` is `failed` whenever `failed + unresolved > 0`.

The named windows, all in the README:

| Window | What can happen | Caught by |
| --- | --- | --- |
| Reply lands between step 3's poll and this deal's claim | one more chase goes out | the next run's poll stops the ladder |
| Reply lands while Python or Docker is down | the webhook is lost (never retried) | the next run's poll |
| Deal dragged to Won between step 7's read and the claim | one more chase goes out | the next run's read |
| Deal moved from Draft to Sent seconds before a run | search may not list it yet | the next run |

### 4.6 Stop sources

One function, `record_stop(deal_id, reason, ref, now=None)`, with `now` defaulting to
`clock.now()`, opens `BEGIN IMMEDIATE`, inserts into `stops` with `INSERT OR IGNORE`, commits, and
returns whether a row was added. All three sources call it. Because the claim (section 4.5) and
`record_stop` both take the SQLite write lock, either the stop commits first and the claim sees it,
or the claim commits first and the send proceeds. Never both silently. That is the whole of the
"race-free on the reply path" claim, and it holds because every writer, thread or process, opens
`BEGIN IMMEDIATE` on the same file on the same host.

**Projection.** When `record_stop` adds a new row with reason `reply`, the caller then PATCHes the
deal to `dealstage = Replied`, outside any transaction. In the run this PATCH is counted in
`hubspot_sync_failed` on failure; in the hook a failure is logged and ignored. Each run also
PATCHes Replied for any candidate whose reply stop **this run's own poll** recorded (section 4.5
step 5), which covers a hook that never fired; a PATCH the hook itself lost is not chased down
later, because a run that re-PATCHed old stop rows would overwrite whatever the owner has done to
the board since. This PATCH is
the only writer of the Replied stage and is what beat 4 times. Stage and manual stops write nothing
to HubSpot.

Nothing deletes a stop row and there is no unstop path, so every stop is final for that quote; the
rule and its reasoning are in section 5.

**(1) Reply.** Two paths, one record:

| Path | Mechanism | Role |
| --- | --- | --- |
| Poll | `GET /api/v1/search?query=to:quotes+` at the start of every run | correctness; also the shape of the documented production adapter |
| Webhook | Mailpit `MP_WEBHOOK_URL=http://host.docker.internal:8000/hooks/mailpit?secret=${CHASE_SHARED_SECRET}`, `MP_WEBHOOK_LIMIT=0` | latency; a reply stops the ladder within seconds instead of at the next run |

The webhook payload is Mailpit's `MessageSummary` (`ID, MessageID, From, To[], ReplyTo[], Subject,
Snippet`, no body), verified(https://raw.githubusercontent.com/axllent/mailpit/develop/server/ui/api/v1/swagger.json),
so the hook needs nothing beyond it. The hook:

- rejects any message with no `To[].Address` whose local part is `quotes+<deal_id>` (the domain is
  not checked; the deal id is the join key) and whose deal id has a row in `reminder_log`; every
  chase the ladder itself sends is a received message that fires this hook, so the filter is
  mandatory, not defensive;
- dedupes on `MessageID` through `UNIQUE (deal_id, ref)`;
- on a new reply row, PATCHes the deal to Replied (the projection above), logging any failure;
- logs an unmatched plus address and returns `202`; the run-start poll is the one place
  `unmatched_replies` is counted, so the number in the report is "unmatched replies currently in
  Mailpit", a state count, not a per-run delta.

Outgoing chases carry `Reply-To: quotes+<deal_id>@<reply_domain>`; RFC 5322 says replies are
suggested to go to `Reply-To` when present,
verified(https://datatracker.ietf.org/doc/html/rfc5322#section-3.6.2). The demo client
(`demo-reply`) does; a real client may reply to `From`, which the production note names. No
sender-email fallback (section 2).

**(2) Stage.** A direct object read of each due deal before any claim (section 4.5 step 7); any
stage in `stop_on.stages` records `stage:<name>`. This path is polled, so the window is one run
interval plus the in-run gap named above; the README says so in those words.

**(3) Manual.** `POST /stop` with body
`{"deal_id": "1234567890", "reason": "manual", "ref": "phone call 2026-09-09"}`. `reason` is an
enum over `reply`, `stage` and `manual`; there is no `paid` value because there are no invoices.
`ref` is optional; when absent the server fills `manual:<stopped_at>`. This is the endpoint a
production adapter (n8n IMAP over Gmail) would post to, and the two-line curl in the README is its
only demonstration; it is not on camera.

### 4.7 HubSpot portal setup

His **existing HubSpot Free portal** is the demo portal, and it is the only one this build touches.
It was emptied on 2026-08-12 (every contact and company deleted) and is believed to carry no custom
properties, but that is a recollection rather than a check, so spike 3 lists the existing custom
properties yet, so the demo spends 2 of the 10 and mixes with nothing real.

Setup, all by hand in the UI (property and stage counts stay visible that way):

| Item | Value |
| --- | --- |
| Pipeline | the single default pipeline; Free permits 1 deal pipeline per account, verified(https://legal.hubspot.com/hubspot-product-and-services-catalog) |
| Stages, renamed in place | Draft, Sent, Chasing, Replied, Won, Lost (rename six of the seven defaults, delete the seventh). Renaming on Free is implied by the absence of a gate, not stated (unverified). Renaming changes a stage's label only; `dealstage` holds the stage id. The app addresses stages by id in every call: on load `hubspot.py` reads `GET /crm/v3/pipelines/deals` once (the deal schema read scope) and builds label to stage id for the single pipeline; search filters, stage comparisons and PATCHes use the id, the YAML keeps labels only |
| Custom property 1 | `quote_sent_at`, Date, deal object; written by the seed, backdated relative to the clock |
| Custom property 2 | `last_chase_at`, Date and time, deal object; written after every `sent` |
| Private app | one, scopes for deal and contact read/write and deal schema read (confirm names in the creation UI); available on Free, verified(https://developers.hubspot.com/docs/api/private-apps) |
| Token | pasted by Jasper into `.env` as `HUBSPOT_TOKEN`; never in chat, never in the repo |

Why these two properties: HubSpot's own "date entered stage" properties are read-only and populated
by stage movement (stated in the audit, unverified against a vendor page), so without
`quote_sent_at` every seeded deal would have entered Sent at seed time and nothing would be due.
Ten custom properties is the cap per account across all objects, not per object,
verified(https://legal.hubspot.com/hubspot-product-and-services-catalog); two leaves eight.

The on-camera "before" filter is a saved list view: `dealstage` in {Sent, Chasing} and
`last_chase_at` is unknown or before 7 days ago. Whether CRM Search filters on custom datetime
properties is not stated in vendor text (unverified); the last spike of evening 1 runs one
`NOT_HAS_PROPERTY` and one `LT` search on `last_chase_at`. Fallback: filter on
a saved view filtered on `dealstage` in {Sent, Chasing} alone, whose count is read aloud while the
age claim comes from the app's own output. `hs_lastmodifieddate` is NOT the fallback: every deal is
seeded minutes before the recording, so "modified before 7 days ago" matches nothing and beat 1
would open on an empty list. The view shows a **count**; dollar sums on
filtered deal views are Sales Hub Starter and above,
verified(https://knowledge.hubspot.com/records/review-data-insights-on-deal-views), so the dollar
figure comes from the app (sum of HubSpot `amount` over the same filter) and is labelled seed data.

API budget is not a concern: 100 requests per 10 seconds per private app, 250,000 per day, search 5
per second, verified(https://developers.hubspot.com/docs/api/usage-details). A run is one search,
one Mailpit poll, and for each due deal one direct read (with associations) and one contact read,
plus one PATCH per send and one per reply-stop repair; sequential, under a hundred calls. Handle
429 with `Retry-After` once, then abort the run (section 6) so the report never reads half-updated.
The batch update endpoint was not verified and is not used.
Only one sort rule per search,
verified(https://developers.hubspot.com/docs/api-reference/latest/crm/search-the-crm), so "oldest
untouched first" for the digest is sorted in Python.

### 4.8 Mailpit

Docker. The repository's `docker-compose.yml` ships **this one service and nothing else**. n8n is
his existing running instance, not a service in this file: a second n8n here would collide with it
on port 5678 and on the container name. The README says so in one line, and tells a stranger who
has no n8n to run their own or to replace the wrapper with cron plus curl.

```yaml
mailpit:
  image: axllent/mailpit:latest        # pin the tag on evening 1 after `docker pull`
  ports: ["1025:1025", "8025:8025"]    # 1025 mapped to the host because Python runs on the host
  environment:
    MP_WEBHOOK_URL: "http://host.docker.internal:8000/hooks/mailpit?secret=${CHASE_SHARED_SECRET}"
    MP_WEBHOOK_LIMIT: "0"              # default is 1 per second and excess is DROPPED, not queued
    MP_SMTP_AUTH_ACCEPT_ANY: "1"
```

Facts the design leans on: the webhook fires on every received message, the default limit is one
request per second with excess ignored (not queued), and failed calls are never retried, all
verified(https://mailpit.axllent.org/docs/integration/webhook/); `MP_WEBHOOK_LIMIT` default 1,
verified(https://mailpit.axllent.org/docs/configuration/runtime-options/); SMTP binds `0.0.0.0:1025`
and can be mapped to the host, verified(https://mailpit.axllent.org/docs/install/docker/); search
filters include `to:`, `from:`, `message-id:`,
verified(https://mailpit.axllent.org/docs/usage/search-filters/). Whether `to:quotes+` matches the
plus address as a substring of the recipient is confirmed by the evening-1 smtplib spike, not
assumed. Any-recipient acceptance is the tool's purpose and is confirmed by the same spike.

Mailpit reaches Python on the host as `http://host.docker.internal:8000`, the same path n8n uses,
verified(https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/common-issues/).

SMTP (1025) and the API (8025) are the same container, so stopping Mailpit takes both down and the
run aborts at the reconciliation poll before it claims anything (section 4.5 step 3). That is why
the ladder reads its SMTP port from `CHASE_SMTP_PORT` (default 1025) instead of hardcoding it:
pointing that variable at a closed port kills sending while the API stays up, which is the only way
to produce a genuinely failed run rather than an aborted one. The failure beat (section 13) uses it.

`demo-reply <quote-label>` resolves the label to a deal id from HubSpot, then sends via `smtplib` to
`localhost` on `CHASE_SMTP_PORT` with `From` = the seeded contact, `To` = `quotes+<deal_id>@<reply_domain>`, a
subject `Re: <last chase subject>`, or `Re: Quote <label>` when no chase has been sent yet, and a
two-line body. An SMTP-received message is verified to fire the webhook; whether
`POST /api/v1/send` does is unverified, so the CLI uses SMTP.

### 4.9 Groq

One call per step-1 send, after the row is claimed, never before. The call is a plain `httpx` POST to
Groq's OpenAI-compatible chat completions endpoint, not the `groq` SDK: `httpx` is already a
dependency for HubSpot and Mailpit, one library does all three, and the SDK's support for Python
3.14 is unchecked. Configuration in `.env`
(`GROQ_API_KEY`, pasted by Jasper) and YAML-independent settings in `chase/groq_opener.py`:

| Setting | Value |
| --- | --- |
| Model | `openai/gpt-oss-120b` via `CHASE_GROQ_MODEL` in `.env`, the default when unset, so a deprecation is a one-line change |
| Response format | `json_schema`, `strict: true`, schema `{"opening_line": string}`, `additionalProperties: false`, supported on both gpt-oss models, verified(https://console.groq.com/docs/structured-outputs) |
| `reasoning_effort` | `low` |
| Timeout | 3 seconds, then fallback |
| Max output | `max_tokens` 512. Reasoning tokens are believed to share the completion budget on gpt-oss, so a small value would truncate before the JSON is emitted and every call would fall back (unverified, see section 9; 512 is the safe side of an unchecked claim and `groq_fallbacks` would show it if wrong).| Python enforces `len(opening_line) <= 160` and rejects any line containing a digit or a currency sign, because a schema cannot bound length |
| Spacing | calls are serialised with at least 2.5 seconds between them; 30 RPM is exactly 2.0 s, and `doctor` spends one call of its own, so the ceiling is not the place to sit |
| Fallback | the step-1 template's fixed opener; every fallback increments `groq_fallbacks` |

Free tier: 30 RPM, 8,000 TPM, 1,000 RPD, 200,000 TPD, no card,
verified(https://console.groq.com/docs/rate-limits), verified(https://console.groq.com/docs/billing-faqs).
Whether reasoning tokens count toward TPM is unverified, so a long run may flip to templates
mid-run; the counter makes that visible rather than preventing it. Model deprecations: nothing
listed after 2026-08-16 touches gpt-oss, verified(https://console.groq.com/docs/deprecations); the
README links that page.

The prompt receives only the client `tone` string and a work category. `opening_line` takes the
category from the deal name: the text between the Q-number and the first comma is matched exactly
against `work_categories` in that client's own YAML; a match passes the listed value into the
prompt, no match uses the template opener and counts a fallback. The list is per client because a
fit-out contractor and an agency do not sell the same things, and because a client's whole
configuration should be one file. The deal name itself, amounts, dates and the contact name never enter the prompt;
amounts, dates and customer names are inserted by `build_message` after the call. This keeps
customer data out of the prompt and removes the prompt-injection surface a deal name would open.

The README's wording: Groq writes one bounded opening line on the first chase only, and
`groq_fallbacks` says how many chases in this run were fully templated, so "AI-written" is a claim
per run, not a standing one.

### 4.10 n8n workflow

n8n is his existing running instance, so this repository pins nothing in a compose file it does not
own. What it pins instead is a fact: on the evening the workflow is pushed, `docker inspect` reads
the image digest that instance is actually running, and that digest goes into the README beside the
push date. Naming a version number nobody checked would be worse than naming none. n8n 3.0 requires
Docker for self-hosting and turns on credential key rotation by default,
verified(https://docs.n8n.io/changelog/v30-breaking-changes), so the runbook carries one line for
`scripts/push_n8n.py`: after a 3.0 upgrade, re-check that the credentials attached by node name
still resolve, and expect to re-create the API key.

n8n is optional for clone-and-run: everything works from a terminal with only Mailpit up, and the
README documents cron plus curl as the replacement. n8n earns its place by doing what a dead Python
process cannot: alert about itself.

`workflows/chase-ladder.json`, pushed by `scripts/push_n8n.py`, his existing n8n API push loop
brought into the repo on evening 3 (idempotent by workflow name, credentials attached by node-name
map at push time so the JSON stays credential-free). `push_n8n.py` reads
`config/<CHASE_CLIENT>.yaml`, substitutes `send_window.tz` into `settings.timezone` and
`digest.day` and `digest.time` into the Friday Schedule Trigger, so the YAML stays the only place
those values are typed. There is one workflow file and one push, then one activate.

| Node | Setting |
| --- | --- |
| Schedule Trigger "Every 30 minutes" | workflow timezone from the YAML `send_window.tz` (`Australia/Sydney` for the live client), written by `push_n8n.py` |
| HTTP Request "Run ladder" | `POST http://host.docker.internal:8000/run-ladder` with body `{}`, header auth credential carrying `X-Chase-Secret`, timeout 120000 ms, `retryOnFail` 3 tries with 5 s wait, on error **continue using error output**, so a 500 (the aborted run), a timeout and a dead Python all leave by the second output instead of ending the execution |
| IF "status != ok" | `{{ $json.status }}` not equal `ok`, on the node's main output |
| Telegram "Alert" | Send Message to the owner's chat id, wired from **both** the IF true branch and the HTTP node's error output: `Chase ladder alert: status={{ $json.status ?? "unreachable" }}, failed={{ $json.failed ?? "?" }}, unresolved={{ $json.unresolved ?? "?" }}, hubspot_sync_failed={{ $json.hubspot_sync_failed ?? "?" }}, groq_fallbacks={{ $json.groq_fallbacks ?? "?" }}, error={{ $json.error ?? "" }}`. The `??` defaults are what let one node serve a run report and a dead process |
| Schedule Trigger "Friday digest" | the YAML `digest.day` at `digest.time`, same timezone, written by `push_n8n.py` |
| HTTP Request "Digest" | `GET http://host.docker.internal:8000/digest`, same header auth |
| Telegram "Digest" | Send Message with `{{ $json.text }}` |

Both HTTP nodes have "continue using error output" on, and **both** error outputs are wired into the
Telegram "Alert" node. Without the second wire a Friday digest that 500s, 401s or meets a dead Python
process is silent, and a failed digest is indistinguishable from a quiet week, which is the exact
failure this project exists to talk about.

One workflow file, one alert node. Routing the HTTP node's error output into the same Telegram node
covers Python being down, a timeout, a 401 and the 500 of an aborted run, without a separate
alerting workflow, a push-order dance to learn its id, or a `doctor` check that two files are still
wired to each other.

The Schedule Trigger only fires from a **published** workflow,
verified(https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.scheduletrigger/);
`doctor` checks the active flag, and the evening-3 checklist says publish, not save, before
recording. The HTTP node, its header auth, its timeout and its error output are standard,
verified(https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/).

### 4.11 Telegram

Free, no card, verified(https://core.telegram.org/bots). A bot cannot message a user first,
verified(https://core.telegram.org/bots); the owner presses Start once and the chat id is read from
`getUpdates`. Setup steps in the README, done by Jasper: create the bot with @BotFather, press
Start, read the chat id, create the n8n Telegram credential (bot token only,
verified(https://docs.n8n.io/integrations/builtin/credentials/telegram/)), put the chat id in
`.env` as `TELEGRAM_CHAT_ID` so the push script substitutes it into the two Send Message nodes
(Alert and Digest); chat id plus text is all they need. Python never holds
the token and never calls Telegram.

### 4.12 Seed and reset

`python -m chase seed` writes 24 deals in the demo portal, a fixed set so every demo starts from
the same board:

| Set | Count | Stage | `quote_sent_at` (days before `clock.now()`) |
| --- | --- | --- | --- |
| Live quotes | 20 | Sent | spread over `[1, 1, 2, 2, 4, 4, 5, 6, 8, 8, 9, 10, 12, 13, 15, 16, 18, 20, 22, 25]` so steps 1, 2 and 3 are all sent on the first run (4, 6 and 6 deals) and six deals are exhausted after it |
| Board dressing | 4 | 1 Draft, 2 Won, 1 Lost | recent |

Each deal: name `Q-04xx <work category>, <customer business>` (for example `Q-0412 website rebuild,
Marrickville Cycles`; the quote number is a display label only, and the
work category is one of the client's `work_categories` values, section 4.9), an `amount` in the
client currency, one associated contact with a name and an email at `customer.example`,
`quote_sent_at` = midnight UTC of (`clock.now()` minus the spread value) as epoch milliseconds,
which is the only form a HubSpot Date property accepts, `last_chase_at` empty. The seed marks its
own rows (the `Q-` prefix and the `customer.example` domain) so `reset` can find them. The total amount is
whatever the seed wrote and is labelled seed data everywhere it appears.

`python -m chase reset` deletes `reminder_log` and `stops`, calls Mailpit's delete-all messages
endpoint, archives the marked deals and contacts, and re-seeds relative to the current clock, so a
demo that sat for a week cannot rot into "success, 0 sends". `doctor` reports the seed's age against
`clock.now()` for the same reason.

### 4.13 doctor

`python -m chase doctor` runs before every recording and fails loudly on any miss:

| Check | Pass condition |
| --- | --- |
| Environment | every required key from `.env.example` (section 4.14) present in `.env` (presence only; values are never printed) |
| Groq | models list call succeeds and contains the configured model; one real step-1 call returns a schema-valid line |
| HubSpot | the pipeline's stage labels include Draft, Sent, Chasing, Replied, Won, Lost, and every name in `stop_on.stages` is one of them; one deal readable with both `quote_sent_at` and `last_chase_at` present in its property set |
| Mailpit | `GET /api/v1/info` returns 200 |
| n8n | the `chase-ladder` workflow exists, is `active`, and a read-back of it shows `settings.timezone` and the Friday trigger's day and hour equal to the client YAML, because a silently dropped timezone would send the digest at the wrong local hour and nothing else would notice (skipped with a warning when `N8N_URL` is unset) |
| Seed | the newest `quote_sent_at` is within the ladder's first step of `clock.now()`, otherwise "seed is N days stale, run reset" |
| SQLite | `journal_mode` reads `wal`; no `claimed` rows older than `unresolved_after` (else "N unresolved rows, resolve by hand"); doctor runs outside a run, so this is the same definition section 6 uses |
| Clock | prints `clock.now()`, the active offset, the resulting local time in `send_window.tz` and whether `in_send_window` is true; false is a failure, because a recording made outside the window sends nothing and reports `skipped_window` |

### 4.14 Repository layout

```
chase-ladder/
  README.md                          claims, seeded-vs-measured (authorship row included), architecture,
                                     race note, projection note, reply-stop-is-final rule, Spam Act
                                     note, production adapter, runbook (n8n image digest and push
                                     date), YAML diff, failure table
  docs/superpowers/specs/            this document
  chase/
    __main__.py  cli.py  app.py      entry points: `python -m chase`, FastAPI routes
    clock.py                         the only datetime.now()
    config.py  window.py             YAML loader, in_send_window()
    db.py  stops.py                  schema, claim(), mark(), record_stop()
    ladder.py  report.py             due_step(), run_ladder(), run report
    hubspot.py  mailpit.py           API clients (pipelines read, search, direct read with associations,
                                     contact read, create and archive for seed/reset, PATCH last_chase_at
                                     + Chasing after a send, PATCH Replied after a reply stop; search
                                     poll, delete-all)
    mailer.py  templates.py          smtplib send, message build
    groq_opener.py                   step-1 opener with fallback and counter
    digest.py  seed.py  doctor.py
  config/harbourline.yaml            live demo client
  config/lakeshore.yaml              second client, tests + README diff only
  templates/harbourline/step{1,2,3}.txt
  templates/lakeshore/step{1,2,3,4}.txt
  workflows/chase-ladder.json        schedule -> run -> IF or the HTTP error output -> Telegram;
                                     Friday -> digest -> Telegram; both HTTP nodes route their
                                     error output to the same alert node; one file, no error workflow
  scripts/push_n8n.py                about 60 lines against POST /api/v1/workflows, PUT for updates,
                                     POST /activate; credential map by node name; timezone and
                                     digest schedule copied from the client YAML at push time
  docker-compose.yml                 mailpit only; n8n is his existing instance, not a service here
  tests/                             section 10
  data/                              chase.db and WAL siblings, gitignored
  .env.example                       CHASE_SHARED_SECRET, HUBSPOT_TOKEN, GROQ_API_KEY, TELEGRAM_CHAT_ID,
                                     and the optional CHASE_CLIENT (default harbourline),
                                     CHASE_CLOCK_OFFSET (default 0d), CHASE_PORT (default 8000),
                                     CHASE_SMTP_PORT (default 1025), CHASE_UNRESOLVED_AFTER
                                     (default 5m), CHASE_GROQ_MODEL (default
                                     openai/gpt-oss-120b), MAILPIT_URL (default http://localhost:8025),
                                     N8N_URL, N8N_API_KEY; names only, no values
  requirements.txt                   fastapi, uvicorn, httpx, pyyaml, tzdata, pytest
  .gitignore                         .env, data/, *.db, *.db-wal, *.db-shm
```

Nothing in the repository can authenticate to anything. The push script reads `N8N_URL` and
`N8N_API_KEY` from `.env` at run time.

### 4.15 Digest

`build_digest(client, now)` runs `run_ladder(client, now, dry=True)` (section 4.1) and reads the
result:

- **Unanswered** = every candidate the search returned (stage Sent or Chasing) that has no `stops`
  row. `unanswered_count` is their number; `unanswered_total` is the sum of their HubSpot `amount`,
  in the YAML `currency`, labelled seed data in the demo.
- A deal is **stale** when `last_chase_at` (or `quote_sent_at` when it has never been chased) is
  more than `digest.stale_after_days` before `now`.
- **Order**: `quote_sent_at` ascending, sorted in Python (section 4.7), so the oldest untouched
  quote is first.
- **Text**: a header `<business_name>: N quotes unanswered, <currency> <total> sitting in them
  (seed data)`; one line per deal, `<dealname>, <amount>, quoted <n>d ago, last chased <n>d ago`
  (or `never`), with `, stale` appended when the threshold is passed; a footer with `candidates`,
  `due`, `unresolved`, `unmatched_replies` and `ladder_exhausted` from the dry pass, and the line
  `as of <now>`. Model health and projection health are per-run facts; they live in the run report,
  where the n8n alert branch already reads them, and the digest does not repeat them.

`status` uses the same dry pass and prints the same footer under its row-count line and table.

## 5. Data model

**Deal pipeline (the single free pipeline, stages renamed):**
Draft, Sent, Chasing, Replied, Won, Lost.

| Stage | Set by | Meaning to the ladder |
| --- | --- | --- |
| Draft | owner | not a candidate |
| Sent | owner (seed in the demo) | candidate; `quote_sent_at` must be set |
| Chasing | the app, on the first `sent` | candidate; the board and the filter agree with the send log |
| Replied | the app, on a reply stop (projection, section 4.6) or the owner by hand | not a candidate while it sits here; whether it can ever chase again is decided by the stop row, not the stage |
| Won, Lost | owner | stop source (2) |

**A stop is final; the stage is not an undo.** Nothing deletes a row from `stops`, and the claim
refuses on any stop row, so once a quote has been replied to, moved to Won or Lost, or stopped by
hand, that quote is finished with the ladder. Dragging the deal back to Chasing does not resume
chasing, and no run will silently drag it back to Replied either, because the projection repair in
section 4.5 step 5 only touches stops recorded during that same run. To chase a customer again the
owner raises a new quote, which is a new deal with its own ladder. A deal the owner parked in
Replied by hand, with no stop row behind it, is the one case that does come back: drag it to Chasing
and it is a candidate again on the next run.

**Custom properties, 2 of the 10 allowed per account:**

| Object | Property | Type | Written by | Purpose |
| --- | --- | --- | --- | --- |
| Deal | `quote_sent_at` | Date | seed (demo) or owner (production) | the date due maths counts from; the only date the seed can backdate |
| Deal | `last_chase_at` | Date and time | the app, after every `sent` | the on-camera filter and the owner's "when was it last chased" |

**What lives where:**

| Fact | Truth | Projection |
| --- | --- | --- |
| A chase for (deal, step) was claimed, sent or failed | `reminder_log` | `last_chase_at` and stage Chasing in HubSpot |
| A deal is stopped, and why | `stops` | stage Replied in HubSpot (reply stops only) |
| The quote's amount, contact, sent date, stage | HubSpot | none |

**Projection note (verbatim into the README):** SQLite is the send-log truth. HubSpot's two
properties are a projection written after the fact; they can lag by one failed PATCH or be missing
after a HubSpot outage, and `hubspot_sync_failed` in the run report says when that happened. If the
two disagree, the send log is right and the next successful run repairs the projection.

The join key everywhere is the HubSpot deal id, which HubSpot never changes. The quote number in the
deal name is a label for humans; a rename never orphans a quote.

## 6. Run report and counters

Returned as JSON by `/run-ladder` and printed by `python -m chase run`:

```json
{
  "client": "harbourline",
  "now": "2026-09-08T22:10:00+00:00",
  "clock_offset": "4d13h",
  "candidates": 19,
  "due": 10,
  "sent": 9,
  "skipped_window": 0,
  "stopped": 1,
  "unresolved": 0,
  "failed": 0,
  "hubspot_sync_failed": 0,
  "groq_fallbacks": 1,
  "unmatched_replies": 0,
  "ladder_exhausted": 6,
  "status": "ok",
  "error": null
}
```

These numbers come from one rehearsal, they are not a promise. The seed writes `quote_sent_at` as
midnight UTC of (`clock.now()` minus the spread value), so the age of every quote carries the
fraction of a day between midnight and the moment of seeding, and a different recording hour moves
some quotes across a step boundary. Two rules follow, and section 11 repeats them: **seed under the
same offset the first recorded run uses**, and **read every count off the screen during the take
rather than saying it in advance**. The walk-through below is still worth reading, because the
relationships hold whatever the absolute counts are, and it is the state machine in
one object. The default seed puts 20 quotes in Sent; in the rehearsal that produced these figures
beat 2's run sent 16 of them (4 at
step 1, 6 at step 2, 6 at step 3), leaving 6 with their last rung logged, which is
`ladder_exhausted: 6`. Beat 4's reply moves one deal to Replied, so at `4d` the search returns 19,
not 20: a replied deal is not in {Sent, Chasing} and is not a candidate at all. Ten of those 19 have
a rung due (4 at step 1, 3 at step 2, 3 at step 3). Beat 5 is the drag: seconds before the run the
owner moves one due deal to Won, and because HubSpot search lags updates by a few moments the search
still hands it over as Chasing, so it counts in `candidates` and in `due`. The direct read of
section 4.5 step 7 then sees Won, writes a `stage:won` stop, and the claim is refused, which is
`stopped: 1` and `sent: 9`. One of the four step-1 sends used the template opener, hence
`groq_fallbacks: 1`.

| Counter | Definition | The silent failure it exposes |
| --- | --- | --- |
| `candidates` | deals in Sent or Chasing after the search | zero means the seed is gone or the token is dead, not "all quiet" |
| `due` | candidates with an eligible step now, stop rows ignored; `stopped` is the subset refused at claim time | zero with many candidates means the seed rotted or the clock is wrong |
| `sent` | rows marked `sent` this run | |
| `skipped_window` | due deals not sent because `in_send_window` was false | a run outside hours reporting `ok, 0 sent` is explained, not mysterious |
| `stopped` | claims refused because a stop row existed | |
| `unresolved` | rows still `claimed` whose `claimed_at`, the wall clock at that claim, is more than `unresolved_after` (default 5 minutes) before now; this also covers a send whose outcome was ambiguous | a crash between commit and send, or an SMTP call that may or may not have delivered |
| `failed` | rows the SMTP server refused outright this run; the step is eligible again on the next run | SMTP down or misconfigured |
| `hubspot_sync_failed` | PATCHes that failed after a `sent` or after a reply stop | the projection lagging |
| `groq_fallbacks` | step-1 sends that used the template | a dead or throttled model hidden by the fallback |
| `unmatched_replies` | messages to `quotes+` that resolve to no known deal (state count) | a reply that would otherwise be silently ignored |
| `ladder_exhausted` | candidates whose last ladder step has a `claimed` or `sent` `reminder_log` row and which have no stop row | the quotes the owner has to phone about |
| `status` | `failed` whenever `failed + unresolved > 0`, else `ok`; an aborted run never reaches the report (below) | the n8n IF branch keys on this |
| `error` | `null` on a completed run; the exception text on an aborted one | |

A HubSpot search or read that fails after its one retry, a Mailpit poll failure, or a config error
aborts the run before the report: the route returns HTTP 500 with
`{"status": "failed", "error": "<message>"}` so the n8n HTTP node leaves by its error output into
the Telegram alert; the CLI exits 1. Nothing is claimed in an aborted run, so an aborted run reports
no counts at all, which is why the on-camera failure beat provokes a failed run rather than an
aborted one (section 13).

The digest prints `candidates`, `due`, `unresolved`, `unmatched_replies` and `ladder_exhausted`
from a dry pass under its own totals. `groq_fallbacks` and `hubspot_sync_failed` are per-run event
counts; they appear in the run report and the Telegram alert only, and no per-run counter is stored.

## 7. Failure handling

| Failure | Behaviour | Surfaced as |
| --- | --- | --- |
| Wrong or missing `X-Chase-Secret` | 401, nothing read or written | n8n HTTP node's error output feeds the Telegram alert |
| Python down when n8n fires | HTTP node fails after 3 tries | error output to the Telegram alert |
| Python down when a reply arrives | webhook lost, never retried | next run's poll records the stop; one more chase possible in between, named in the README |
| SMTP unreachable mid-run, Mailpit's API still up (`CHASE_SMTP_PORT` at a closed port, or the SMTP listener alone gone) | `smtp_send` raises per deal, rows marked `failed`, run continues to the next deal | report `status: failed`, `failed = N`, Telegram alert via the IF branch; this is the on-camera failure beat |
| Whole Mailpit container stopped | the reconciliation poll fails at step 3, so the run aborts before it claims anything | HTTP 500 / exit 1, error output to the Telegram alert, and no counts in the message, which is why the failure beat does not use this route |
| A rung was marked `failed` on an earlier run | the next run's `due_step` treats that step as eligible again and the claim's `ON CONFLICT` takes the row over, unless a higher rung has since come due | the retry appears as an ordinary `sent`; `reminder_log` row count is unchanged |
| Process killed between claim `COMMIT` and `mark` | row stays `claimed` | `unresolved >= 1`, `status: failed` on every run until a human resolves it; never resent |
| Same run triggered twice (n8n retry, or a manual `run` during the schedule) | the second claim reaches `ON CONFLICT DO UPDATE`, whose `WHERE status = 'failed'` matches nothing against a `claimed` or `sent` row, so `rowcount 0` | zero new rows; a sequential re-run reports `due 0, sent 0`; in overlapping runs the loser's claims return `ALREADY_LOGGED`, are not counted, and the counter inequality in section 10 allows the gap |
| Mailpit poll fails at run start | run aborts before any claim | HTTP 500 / exit 1, error output to the Telegram alert |
| `/stop` lands during a run | `record_stop` waits on the write lock up to 5 s, then commits; the next claim for that deal sees it | either the stop wins or that one send is logged; never both silently |
| Reply to `quotes+<id>` for a deal that does not exist | no stop row | `unmatched_replies` |
| Reply sent to `quotes@` without the plus part | not seen by the `to:quotes+` poll, rejected by the hook's To-filter | not detected; the production note names plus-address routing as a requirement |
| Customer replies to the **original quote email** rather than to a chase | that email came from the business's own mailbox, and unless it carried `Reply-To: quotes+<deal_id>@...` the reply never reaches an address the ladder watches | not detected; the ladder keeps chasing until someone posts `/stop`. In production the quote email itself must carry the plus-address `Reply-To`, or the stop for that first reply is a manual one |
| Own outgoing chase hits the webhook | To-filter rejects (To is the customer) | nothing; tested with one payload per (seeded deal, step), 60 in all |
| Same reply seen by webhook and poll | second insert ignored by `UNIQUE (deal_id, ref)` | one stop row |
| Deal moved to Won after the run-start read | claim proceeds, one more chase | next run's read stops it; named in the README |
| HubSpot 429 | sleep `Retry-After`, retry once | on second failure the run aborts: HTTP 500 / exit 1, error output to the Telegram alert |
| HubSpot PATCH fails after `sent` or after a reply stop | SQLite row stays `sent` (or the stop row stays) | `hubspot_sync_failed`; next successful run repairs the projection |
| Groq 400, 429, timeout, over-length or digit in the line, or a deal name whose category is not in the enum | template opener used | `groq_fallbacks` |
| Run outside the send window | nothing claimed; stop rows from the poll stand | `skipped_window`, `status: ok` |
| `tzdata` missing | `ZoneInfo` raises at config load | `doctor` and `run` fail before any send |
| Seed older than the ladder's first step | the youngest deals are already due on the first run, so the step 1 then step 2 progression is gone from the demo; after 14 days every deal is on step 3 | `doctor` says "seed is N days stale, run reset"; `ladder_exhausted` equals `candidates` after one run in the 14-day case |
| n8n workflow saved but not published | schedule never fires | `doctor` reports `active: false` |
| n8n API key rotated or expired | push script 401 | runbook line beside the recorded image digest |
| `datetime.now()` added outside `clock.py` | `test_no_datetime_now_outside_clock` fails | CI red before the offset can silently diverge |

## 8. Seeded versus measured, and the honesty rules

The README carries this table as its second section, straight after the claims paragraph. The Loom
introduces the seed as "here is the situation I set up". The last row applies the same rule to
authorship: a reader can see which files Jasper typed and which Claude did, instead of the repository
being silent about it.

| SEEDED (Jasper wrote it) | MEASURED (the system produced it) |
| --- | --- |
| Every dollar figure, including the "sitting in unanswered quotes" total | Emails sent in a run (`sent`, matched against Mailpit's count) |
| Every quote count, including the "before" filter count | Rows added on the re-run: 0 (`python -m chase status` before and after) |
| "Untouched for 7+ days", because `quote_sent_at` was backdated | Seconds from `demo-reply` to the deal showing Replied in HubSpot, with the mechanism named (webhook, or poll interval) |
| Harbourline Digital and Lakeshore Fitout, both fictional | Wall time for the run, printed by the run itself. The comparison figure, minutes to write one chase by hand, is **hand-timed by Jasper on camera**, so it is stated that way and never as a machine measurement |
| The customer reply ("I am playing the customer"), and the drag to Won | `failed` count and the Telegram alert when `CHASE_SMTP_PORT` points at a dead port |
| Written by Jasper's own hand: `in_send_window()` in `chase/window.py` and `claim()` in `chase/db.py` | Written by Claude: the tests for those two functions, which came first, and every other module in `chase/` |

Rules for every README sentence and every Loom sentence:

1. Never "exactly-once". Say: at most one successful send per quote and step; re-runs add zero rows;
   a send SMTP refused is marked failed and may be retried on a later run; a claim whose outcome is
   unknown is reported as unresolved, with a count, and is never retried automatically.
2. Never "cannot race" unqualified. Say: race-free on the reply path because both writers hit SQLite
   under `BEGIN IMMEDIATE`; the Won/Lost path is a direct HubSpot read of each due deal before any
   claim, once per run, with an inherent window, and the README names it.
3. Reply-to-stop latency is always quoted with its mechanism: seconds when the webhook delivered it,
   the run interval when the poll did. Both numbers appear in the README.
4. The pitch is quotes, not money owed: "which quotes are still unanswered and how much is sitting
   in them". No sentence mentions invoices, payments or receivables.
5. On camera: "I am playing the customer" before `demo-reply`, and "I am fast-forwarding the
   calendar, nothing else is faked" before the clock offset changes.
6. SQLite is the send-log truth; HubSpot's two properties are a projection that can lag or fail, and
   `hubspot_sync_failed` says when it did.
7. n8n is the scheduler and notifier, replaceable by cron plus curl; ladder decisions are in Python
   and tested.
8. Groq writes one bounded opening line on the first chase only; amounts, dates and names never
   enter the prompt; the fallback count is printed.
9. Production adapters (n8n IMAP over Gmail posting to `/stop`, plus-address routing on the
   receiving mailbox, sender-address ambiguity surfaced rather than guessed) are labelled
   documented-not-demoed. Plus-address handling is implementation-specific,
   verified(https://datatracker.ietf.org/doc/html/rfc5233), so the note states it as a requirement
   on the receiving side.
10. Every vendor limit carries verified(url) or unverified, matching section 9; unverified items are
    stated as assumptions with their fallback.
11. A stop is final for that quote. Say: a reply, a Won or Lost move, or a manual stop ends the
    ladder for that quote for good; moving the deal back to Chasing does not resume chasing; to
    chase again the owner raises a new quote.
12. Reply detection depends on the plus address. Say: only a reply that reaches
    `quotes+<deal_id>@<reply_domain>` stops the ladder, so in production the original quote email
    must itself carry that `Reply-To`; otherwise the first reply is a manual stop.
13. Every chase carries the sending business's identity and an unsubscribe line, because the
    Australian Spam Act 2003 requires both on a commercial electronic message,
    verified(https://www.acma.gov.au/avoid-sending-spam).
14. No statistic about how often businesses fail to follow up quotes appears anywhere unless it
    comes from a primary source that was actually read and is cited. A number from a marketing blog
    quoting another blog does not go in the README or the Loom, and none is carried in this
    document.

## 9. Verified facts

Built from the audit's fact-check. "verified" means the sentence was read on the linked vendor
page on 2026-08-29; rows tagged "design read" were read during the design and are not in the
audit; "unverified" rows carry a fallback.

| Claim | Status | URL | Consequence for this design |
| --- | --- | --- | --- |
| CRM Search supports `HAS_PROPERTY`, `NOT_HAS_PROPERTY`, `LT`/`GT`/`BETWEEN` with epoch ms, and one sort rule per search | verified | https://developers.hubspot.com/docs/api-reference/latest/crm/search-the-crm | the before/after filter is buildable; "oldest first" is sorted in Python |
| Those filters and sorts work on custom datetime properties | unverified | https://developers.hubspot.com/docs/api-reference/latest/crm/search-the-crm | evening-1 spike; fallback is a stage-only saved view, never `hs_lastmodifieddate`, which matches nothing against freshly seeded deals |
| Search results lag creates and updates by "a few moments" | verified | https://developers.hubspot.com/docs/api-reference/latest/crm/search-the-crm | Won/Lost stop uses a direct object read; the on-camera after-search waits, never faster than 5/s |
| Free portals can create private apps with CRM scopes; 100 requests/10 s per app, 250,000/day | verified | https://developers.hubspot.com/docs/api/private-apps | one App Token in `.env`; no OAuth app |
| Free: 1 deal pipeline, 10 custom properties per account across objects, 2 users, 1,000 contacts | verified | https://legal.hubspot.com/hubspot-product-and-services-catalog | two properties; one pipeline; second client cannot run live |
| Stages on the default pipeline can be renamed on Free | unverified (no gate stated) | https://legal.hubspot.com/hubspot-product-and-services-catalog | verify in the UI; the app addresses stages by id from the pipelines read, so a rename is a label change only |
| "Date entered stage" properties are read-only and set by stage movement | unverified (audit rationale, no page read) | none | `quote_sent_at` must be a custom property |
| Filtered dollar sums on deal views are Sales Hub Starter and above | verified | https://knowledge.hubspot.com/records/review-data-insights-on-deal-views | on-camera before/after is a count; dollars come from the app; the slug is confirmed to resolve during the evening-1 spikes before the README copies it |
| Search endpoints: 5 requests/s, 200 per page, 10,000 cap | verified | https://developers.hubspot.com/docs/api/usage-details | one page covers the demo; 429 handled with `Retry-After` |
| Mailpit webhook: default 1 call/s, excess dropped not queued, failed calls never retried, 5 s client timeout | verified | https://mailpit.axllent.org/docs/integration/webhook/ | `MP_WEBHOOK_LIMIT=0`; poll is the correctness path |
| `MP_WEBHOOK_LIMIT` default 1 | verified | https://mailpit.axllent.org/docs/configuration/runtime-options/ | set to 0 in compose |
| Webhook payload is `MessageSummary` with `To[]`, `ReplyTo[]`, `MessageID`, no body | verified | https://raw.githubusercontent.com/axllent/mailpit/develop/server/ui/api/v1/swagger.json | To-filter and dedupe work from the webhook alone |
| Mailpit search filters `to:`, `from:`, `message-id:` | verified | https://mailpit.axllent.org/docs/usage/search-filters/ | run-start poll query |
| Mailpit SMTP binds 0.0.0.0:1025, mappable to the host; accepts any credentials with `MP_SMTP_AUTH_ACCEPT_ANY` | verified | https://mailpit.axllent.org/docs/install/docker/ | Python on the host sends to `localhost:1025` |
| Mailpit accepts any recipient domain | unverified (it is the tool's purpose) | https://mailpit.axllent.org/docs/install/docker/ | confirmed by the evening-1 smtplib spike |
| Replies go to `Reply-To` when present | verified | https://datatracker.ietf.org/doc/html/rfc5322#section-3.6.2 | plus address on `Reply-To` |
| `+` subaddressing is a Sieve extension, implementation-specific | verified | https://datatracker.ietf.org/doc/html/rfc5233 | production note states the receiving-side requirement |
| Groq Free: 30 RPM, 8,000 TPM, 1,000 RPD, 200,000 TPD for both gpt-oss models | verified | https://console.groq.com/docs/rate-limits | serialised calls, step 1 only, fallback counted |
| Groq Free needs no card | verified | https://console.groq.com/docs/billing-faqs | $0 holds |
| Reasoning tokens count toward TPM | unverified | https://console.groq.com/docs/rate-limits | fallback may fire mid-run; counter shows it |
| Strict JSON schema supported on gpt-oss-20b and gpt-oss-120b | verified | https://console.groq.com/docs/structured-outputs | `strict: true`, length enforced in Python |
| No deprecation after 2026-08-16 touches gpt-oss; llama-3.3-70b shut down 2026-08-16 | verified | https://console.groq.com/docs/deprecations | model id is config; README links the page |
| SQLite WAL: one writer at a time, readers do not block writers, same host only | verified | https://www.sqlite.org/wal.html | Python on the host, `data/` on a local disk |
| DEFERRED read-then-write fails with `SQLITE_BUSY_SNAPSHOT`; `BEGIN IMMEDIATE` is the remedy | verified | https://www.sqlite.org/isolation.html | every write opens `BEGIN IMMEDIATE` |
| `SQLITE_BUSY_SNAPSHOT` does not invoke the busy handler | verified | https://www.sqlite.org/rescode.html | busy_timeout alone would not have saved the claim |
| Python `sqlite3` default transaction mode is `DEFERRED` | verified | https://docs.python.org/3/library/sqlite3.html | hand-written `BEGIN IMMEDIATE` |
| Python `sqlite3`: timeout 5.0 s, `check_same_thread=True`, `autocommit` parameter available | design read of https://docs.python.org/3.14/library/sqlite3.html, not in the audit | https://docs.python.org/3.14/library/sqlite3.html | `autocommit=True`; one connection per request |
| `ZoneInfo` needs `tzdata` on Windows | verified | https://docs.python.org/3/library/zoneinfo.html | `tzdata` in `requirements.txt` |
| n8n Schedule Trigger fires only from a published workflow; timezone from workflow settings | verified | https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.scheduletrigger/ | publish before recording; `doctor` checks `active` |
| n8n HTTP Request node: header auth, timeout, retry | verified | https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/ | the wrapper needs no code node |
| Inside Docker, `localhost` is the container; use `host.docker.internal` on Docker Desktop | verified | https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/common-issues/ | all container-to-Python URLs use it |
| n8n Telegram credential is the bot token only | verified | https://docs.n8n.io/integrations/builtin/credentials/telegram/ | chat id substituted at push time |
| n8n 3.0 breaking changes: self-hosted requires Docker, and credential key rotation is enabled by default | verified | https://docs.n8n.io/changelog/v30-breaking-changes | no version number is guessed; the digest of the running image is recorded in the README at push time |
| Credentials attached by node name under 3.0's new defaults | verified | https://docs.n8n.io/changelog/v30-breaking-changes | one runbook line for `scripts/push_n8n.py`: after a 3.0 upgrade, re-check the credential map and expect to re-create the API key |
| Telegram bots are free; a bot cannot message a user first; about 1 msg/s per chat | verified | https://core.telegram.org/bots and https://core.telegram.org/bots/faq | owner presses Start; one digest a week is nothing |
| `POST /api/v1/send` fires the webhook | unverified | https://mailpit.axllent.org/docs/integration/webhook/ | `demo-reply` uses SMTP instead |
| Australian Spam Act 2003: consent, plus identification of the authorising business by legal name (or name and ABN), plus an unsubscribe needing no extra details or account | verified | https://www.acma.gov.au/avoid-sending-spam | every chase template carries the business identity and a reply-STOP unsubscribe line; the README names the obligation |
| ServiceM8 lists "Follow up issued quotes with automated emails & texts" across its plans, which run Free (30 jobs a month), Starter AUD 29, Growing AUD 79, Premium AUD 149 | verified, page read 2026-08-29 | https://www.servicem8.com/au/pricing | the demo businesses are a CRM-quoting agency and a commercial fit-out contractor, not a trade |

## 10. Testing

`pytest`, offline by default: HubSpot, Mailpit, Groq and SMTP are faked with in-memory doubles that
record calls; one marker `live` gates the few tests that need Docker up. Every test below is named
so the README can quote it.

**Core (evening 1)**

| Test | Asserts |
| --- | --- |
| `test_config_loads_both_yamls` | both files load, schema-valid, and differ in `send_window.days`, `send_window.tz`, ladder days, ladder length (three versus four steps), tone and `digest.stale_after_days` |
| `test_due_steps[harbourline,lakeshore]` (parametrised over both YAMLs and a table of ages) | the due step for a quote aged N days is the highest eligible step above the highest logged step |
| `test_highest_due_step_only` | a 10-day-old Harbourline quote on its first run gets step 2, not steps 1 and 2 |
| `test_rerun_adds_zero_rows` | two consecutive runs on the same fixtures; the second reports `sent 0`, `stopped 0`, and `reminder_log` row count is unchanged |
| `test_now_local_sydney_spring_forward` | `2026-10-03T15:59Z` reads 2026-10-04 01:59 AEST; `2026-10-03T16:00Z` reads 03:00 AEDT (02:xx never exists) |
| `test_now_local_chicago_fall_back` | `2026-11-01T06:30Z` reads 01:30 CDT, `07:30Z` reads 01:30 CST, `08:30Z` reads 02:30 CST |
| `test_send_window_sydney_2026_10_04` | (a) test-only window `{days: [sun], start: 03:00, end: 04:00, tz: Australia/Sydney}`: `2026-10-03T15:30Z` (01:30 AEST) False, `2026-10-03T16:00Z` (the instant 02:00 AEST becomes 03:00 AEDT) True, `2026-10-03T16:30Z` (03:30 AEDT) True, `2026-10-03T17:00Z` (04:00 AEDT) False; (b) the Harbourline window across the change: `2026-10-01T22:00Z` (Fri 08:00 AEST) True, `2026-10-01T21:59Z` False, `2026-10-04T21:00Z` (Mon 2026-10-05 08:00 AEDT) True, `2026-10-04T20:59Z` False |
| `test_send_window_chicago_2026_11_01` | (a) test-only window `{days: [sun], start: 01:00, end: 02:00, tz: America/Chicago}`: `2026-11-01T05:30Z` (00:30 CDT) False, `2026-11-01T06:30Z` (01:30 CDT, first pass) True, `2026-11-01T07:30Z` (01:30 CST, second pass) True, `2026-11-01T08:30Z` (02:30 CST) False; (b) the Lakeshore window across the change: `2026-10-30T14:00Z` (Fri 09:00 CDT) True, `2026-10-30T13:59Z` False, `2026-11-02T15:00Z` (Mon 09:00 CST) True, `2026-11-02T14:59Z` False |
| `test_send_window_weekend` | Harbourline Saturday is outside; Lakeshore Saturday 10:00 is inside |
| `test_skipped_window_counted` | a run at 22:00 Sydney claims nothing and reports `skipped_window == due` |
| `test_claim_rejects_stopped_deal` | a stop row makes `claim` return `STOPPED` and insert nothing |
| `test_claim_unique_per_deal_step` | a second `claim` for the same pair returns `ALREADY_LOGGED` against a `claimed` row and against a `sent` row, and writes nothing |
| `test_failed_rung_retries_next_run` | a step-1 row left `failed` by a raising SMTP double is eligible again on the next run: the claim's `ON CONFLICT` takes the row over, `claimed_at` and `message_id` are the new ones, `sent_at` is `NULL` again, the row count is still one, and the second attempt reports `sent 1`; with a higher rung due instead, the ladder moves on and the failed rung is not retried |
| `test_claimed_at_is_wall_clock_not_run_start` | a claim is stamped with the wall clock at the claim, not the run's fixed `now`; a row claimed seconds ago reports `unresolved 0` even when a second run begins between the claim and the send, and the same row reports `unresolved 1` once the clock advances past `unresolved_after` |
| `test_message_id_written_by_claim` | after the claim commits and before any send, the row's `message_id` is non-null and equals the `Message-ID` header the send then carries |
| `test_crash_after_commit_marks_unresolved` | SMTP double raises `SystemExit`-like abort after the claim commit; the next run reports `unresolved 1`, `status failed`, and does not resend |
| `test_smtp_refused_marks_failed` | the SMTP double raises a refusal (connection refused, recipient rejected); the row is `failed`, the report says `status failed`, and the run continues to the next deal |
| `test_ambiguous_smtp_leaves_row_claimed` | the SMTP double raises a timeout after the server would have accepted; the row stays `claimed`, `failed` is 0, the next run does NOT resend that step, and once the row is older than `unresolved_after` it is reported as `unresolved` |
| `test_threaded_stop_during_run` | a thread posts `record_stop` while the run iterates; for the targeted deal either no `reminder_log` row exists or exactly one `sent` row exists, never a send after a stop that committed first |
| `test_claim_takes_write_lock_at_begin` | with connection A inside `claim()` after `BEGIN IMMEDIATE` and before `COMMIT`, a second connection opened with `timeout=0.2` raises `OperationalError` (database is locked) on its own `BEGIN IMMEDIATE`; `A.in_transaction` is True inside and False after `COMMIT`; `A.autocommit` is True |
| `test_no_datetime_now_outside_clock` | greps `chase/` for `datetime.now(`, `datetime.utcnow(`, `date.today(` and `time.time(`; only `clock.py` matches |
| `test_status_failed_when_unresolved` | `failed + unresolved > 0` gives `status failed`, else `ok`; `unresolved` counts only `claimed` rows older than `unresolved_after`, so a healthy overlapping run never raises the flag |
| `test_report_counters_consistent` | `sent + failed + stopped + skipped_window <= due`, and every counter is present, `error` included |
| `test_ladder_exhausted_counted` | a deal with only a `sent` step-3 row and no stop counts once; a deal with steps 1 and 2 logged does not; a deal with step 3 logged and a stop row does not; a deal whose step-3 row is `failed` does not, because that rung is still retryable |

**Integrations (evening 2)**

| Test | Asserts |
| --- | --- |
| `test_reply_stops_ladder_webhook_disabled` | with no webhook call at all, a message to `quotes+<id>@` in the Mailpit double stops the deal at the next run through the poll |
| `test_hook_rejects_own_chases` | one webhook payload per (seeded deal, step), 60 in all, shaped like the ladder's own outgoing chases, stops nothing |
| `test_hook_dedupes_message_id` | the same reply delivered twice by the hook and once by the poll yields one stop row |
| `test_unmatched_reply_counted` | a reply to `quotes+999999@` counts as `unmatched_replies 1` and stops nothing |
| `test_stage_won_stops_before_claim` | the direct-read double reports Won while the search double still says Chasing; no chase is sent, one `stage:won` stop row |
| `test_replied_dropped_without_row` | a due deal whose direct read says Replied and which carries no stop row is dropped from the run: no `reminder_log` row, no stop row, no PATCH; moved back to Chasing it is due again |
| `test_reply_stop_is_final` | a deal with a reply stop, dragged back to Chasing, is never chased again: the claim returns `STOPPED` on every later run, and no run PATCHes it back to Replied |
| `test_reply_stop_projects_replied` | a reply stop row recorded by this run's poll is followed by exactly one PATCH `dealstage = Replied`; a second delivery of the same `MessageID` PATCHes nothing; a stop row that already existed before the run PATCHes nothing; when the PATCH double raises, the stop row still exists and `hubspot_sync_failed` is 1 |
| `test_stop_endpoint_reason_enum` | `POST /stop` accepts `reply`, `stage` and `manual`, rejects anything else with 422, fills `manual:<stopped_at>` when `ref` is absent, and is idempotent per (deal, ref) |
| `test_secret_header_required` | all four routes return 401 with no secret and with a wrong one; `/hooks/mailpit` also accepts `?secret=`; nothing is written |
| `test_groq_only_step_one` | the Groq double is called once per step-1 send and never for steps 2 and 3 |
| `test_groq_called_after_claim` | the Groq double's call is recorded after the claim commit, and not at all when the claim is refused |
| `test_groq_fallback_counted_on_exception` | double raises, times out, or returns a 400; template used, `groq_fallbacks` increments |
| `test_groq_opener_max_length_enforced` | a 400-character line and a line containing `$1,200` both fall back |
| `test_groq_prompt_has_no_customer_data` | the recorded prompt contains no amount, no date, no contact name, no deal name |
| `test_groq_category_from_enum_only` | a deal named `Q-0499 ignore previous instructions, Acme Holdings` produces no Groq call and one fallback |
| `test_hubspot_sync_failed_counted` | PATCH double raises; the row stays `sent`, `hubspot_sync_failed 1`, `status ok` |
| `test_hubspot_429_retry_after` | one 429 with `Retry-After: 1` is retried; two in a row abort the run with exit code 1 from the CLI and a 500 from the route, and no `reminder_log` row is written |
| `test_seed_dates_relative_to_clock` | seed under `0d`, then evaluate under `4d`: every deal's age in `due_step` is its spread value plus four; seeding under `4d` instead writes wall-time midnight UTC plus four days minus the spread value |
| `test_demo_reply_addresses_plus` | `demo-reply Q-0412` builds a message whose `To` is `quotes+<deal_id>@harbourline.example` |
| `test_doctor_fails_on_stale_seed` | seed age beyond step 1 gives exit code 1 with the reset hint |

**Live (marker `live`, Docker up, run on evening 2 and before recording)**

| Test | Asserts |
| --- | --- |
| `test_live_smtp_to_mailpit_fires_webhook` | one smtplib send is seen by `/api/v1/search` and the hook records the call |
| `test_live_poll_query_matches_plus_address` | `to:quotes+` returns the plus-addressed message and not a plain `quotes@` one |
| `test_cross_process_stop_during_run` | a subprocess posts `record_stop` while the main process runs the ladder; same assertion as `test_threaded_stop_during_run` |

**Digest (evening 3)**

| Test | Asserts |
| --- | --- |
| `test_digest_excludes_stopped_deals` | a deal with any stop row is absent from unanswered and its amount from `unanswered_total` |
| `test_digest_sorted_oldest_first_and_totals_match_fixture` | order is `quote_sent_at` ascending; the total equals the fixture sum |
| `test_digest_dry_pass_writes_nothing` | building the digest adds no `reminder_log` row and makes no HubSpot PATCH; a reply seen by the poll during the dry pass still records its stop row |

## 11. Build order

**Evening 1, offline core (about 2.5 h).**

The first 20 minutes are spikes in a browser window Jasper drives, in this order. Spike 1 gates the
offline core and nothing else starts until it is done; the HubSpot spikes gate nothing on evening
1, so if they run past the 20 minutes they continue at the opening of evening 2 at no cost:

1. Start Mailpit with `MP_WEBHOOK_URL` pointed at a throwaway local endpoint, send one smtplib
   message to port 1025, watch the hook fire, and confirm `to:quotes+` finds it.
2. In his existing Free portal, rename the default pipeline's stages to Draft, Sent, Chasing,
   Replied, Won, Lost.
3. Create `quote_sent_at` (Date) and `last_chase_at` (Date and time).
4. Create the private app with the deal, contact and deal-schema scopes; Jasper pastes the token
   into `.env` as `HUBSPOT_TOKEN`.
5. Run one `NOT_HAS_PROPERTY` and one `LT` search on `last_chase_at` from a REST client, and
   confirm the data-insights knowledge-base slug in section 9 resolves. Fallback:
   a stage-only saved view for the on-camera filter (section 4.7).

Then write the README claims paragraph (section 1) into `README.md` before any schema. Then build:
`clock.py`; the config loader, then the tests for `in_send_window` and for `claim` before either
function exists, because Jasper types both by hand against them (section 12); the SQLite schema with
WAL, busy_timeout and
`BEGIN IMMEDIATE`; the ladder as pure functions (`due_step` from `quote_sent_at` and the YAML);
claim-then-send over smtplib; the three Harbourline and four Lakeshore templates; the run report;
CLI `run`, `status`, `reset` (SQLite half). Core tests pass.

**Evening 2, integrations (about 3 h).**

Open with seed and reset (they gate every on-camera beat), then the HubSpot client (pipelines read
for the label-to-id map, search Sent/Chasing by stage id, direct read with associations, contact
read, PATCH `last_chase_at` and stage Chasing, PATCH Replied on a reply stop, 429 `Retry-After`
then abort). Reconciliation: the Mailpit `to:quotes+` poll at run start and the Won/Lost direct
read before any claim both write stops first. Routes `/hooks/mailpit` (To-filter, Message-ID
dedupe, Replied projection) and `/stop` (reason enum) behind the shared-secret header.
`demo-reply`. Groq step-1 opener with strict schema, low reasoning, 3 s timeout, counted fallback,
called after the claim. `doctor`, every check except the n8n one, which is written on evening 3
once the workflow exists. Integration and live tests pass.

**Evening 3, wrapper, docs, record (about 3 h).**

`/digest` and `build_digest` (section 4.15) with the digest tests. The single n8n workflow JSON file,
pushed by `scripts/push_n8n.py`, published, committed, with the running image's digest read by
`docker inspect` and written into the README; `doctor`'s n8n check. Telegram bot via
@BotFather, Start pressed, chat id into `.env`, n8n Telegram credential. README: claims,
seeded-versus-measured table with its authorship row, why the stop check is race-free on the reply
path and polled on the stage path, projection note, the reply-stop-is-final rule, the Spam Act note,
production adapter note, the one line telling a stranger with no n8n to run their own or use cron
plus curl, runbook (n8n image digest and push date, key rotation, `tzdata`,
`host.docker.internal`, publish not save, `doctor` before recording), YAML diff, failure table;
the failure table and the verified-facts table are copied from sections 7 and 9 of this document,
not written fresh.

Run `doctor`. **The clock offsets carry hours, not whole days, and the reason is the send window.**
Harbourline's window is 08:00 to 18:00 Sydney, which is 06:00 to 16:00 in Manila, so a Philippine
evening is outside it and every recorded run would report `skipped_window` with zero sends. Whole-day
offsets also drag the Sydney weekday across the weekend: from a Monday anchor, `0d`, `4d` and `8d`
land Monday, Friday and Tuesday, all inside `days`. So: **record from a Manila Monday evening, and
use offsets with an hour component that lands each run inside Sydney business hours**, for example
`13h`, `4d13h` and `8d13h` when recording at 21:00 Manila (23:00 Sydney the same day plus 13 hours
is 12:00 the next Sydney day, inside the window on a weekday). `doctor` prints
`in_send_window(clock.now(), client.send_window)` beside its Clock line and fails when it is false,
so this is caught before the camera rolls rather than in the edit. **Run `reset` under the first
recording offset**, so the seed and the first run share a clock and the ladder steps land where the
rehearsal put them. Recording order, six beats in
three clock positions:

- **Offset `13h`, uvicorn started there.** Beat 1 is the saved list view's count. Beats 2 and 3 run
  from the CLI: the first run sends the due chases (about 16 of the 20, spread across all three
  ladder steps, read the real number off the screen), the second adds no rows. Beat 4 is `demo-reply`
  through the webhook, timed against the deal turning Replied.
- **Offset `4d13h`, uvicorn restarted** (the section 8 rule 5 sentence is said here, and `doctor`'s
  Clock line shows the offset). Beat 5: drag one due deal to Won in HubSpot, then immediately
  execute the n8n workflow by hand from the canvas rather than waiting for the 30-minute tick. The
  report has the shape printed in section 6, `stopped 1` with the dragged deal untouched, and
  the dragged deal is not chased. The deal dragged in beat 5 is the one seeded at spread value 6, so
  the arithmetic below is reproducible: it is due at `4d`, which is what makes `stopped 1` visible,
  and it is out of the ladder by `8d`. Beat 6 begins here: execute the Friday digest branch by hand and
  read the Telegram message off the phone, the unanswered count and the seeded total sitting in
  them, both read from the message rather than recited.
- **Offset `8d13h`, `CHASE_SMTP_PORT` moved to a closed port, uvicorn restarted once for both.** The
  failure beat closes beat 6. Mailpit stays up, so the poll succeeds and this is a real run: the
  rungs due at `8d13h` are claimed, every send is refused by the closed port, and the report reads
  `sent: 0` with `failed` equal to that count and `status: failed`, and the IF branch alerts the same
  phone. A closed port is a connection refusal, which is the one SMTP outcome that marks rows
  `failed` rather than leaving them `claimed`, so the same beat also demonstrates that those rungs
  are retried when SMTP comes back. Read the count off the screen rather than saying it in advance. Stopping the Mailpit container instead would
  take the API on 8025 down too, abort the run at section 4.5 step 3, and put an alert with no
  counts on screen.

Rehearse the whole sequence once with SMTP healthy, then record.

Implementation is planned as three plans, one per evening, each ending at its own test gate:
plan 1 = spikes, clock, config, window, schema, claim, ladder, report, CLI; plan 2 = HubSpot
client, seed and reset, reconciliation, routes, demo-reply, Groq, doctor; plan 3 = digest, n8n JSON
and push script, Telegram, README, recording.

**If evening 3 overruns**, defer in this order: the README's YAML diff section first, `doctor`'s n8n
check second. Nothing else is deferrable. The digest branch is no longer on that list, because the
digest is the pitch's own headline and beat 6 films it arriving on the phone; the alert branch is
the other half of beat 6; and the seeded-versus-measured table is what makes every other beat
honest.

## 12. What only Jasper does

Claude drives the browser and writes most of the code; these actions are Jasper's hands only:

- types his HubSpot password into the browser window Claude drives;
- pastes the HubSpot App Token and the Groq key into `.env` (never into chat, never into the repo);
- creates the Telegram bot with @BotFather and presses Start in its chat;
- creates the n8n Telegram credential;
- pushes to GitHub (Claude commits after showing the diff and getting an OK; a guardrails hook
  blocks Claude from pushing);
- records the Loom.

Two functions are also his hands, by default rather than by option: `in_send_window()` and the
`BEGIN IMMEDIATE` claim transaction. Claude writes their tests first, so he types each function
against a suite that is already failing, and Claude writes everything else in the package. The
reason is not ceremony. The market he is aiming at screens for a programming background, and the
first interview question about this repository is "explain your claim transaction without notes";
the honest answer to that question is much shorter when he wrote the transaction. The
seeded-versus-measured table in section 8 records which files went which way. He can veto this and
have Claude write both, in which case that table row says so.

## 13. Acceptance

The build is done when all of the following hold:

1. Every test in section 10 passes, the live trio included, on the machine that records.
2. The six on-camera beats work end to end against the live demo portal:
   1. the filtered HubSpot list view shows the "before" count;
   2. `python -m chase run` (or the n8n schedule) sends the due chases, the opening lines are
      visible in Mailpit, and the run report's `groq_fallbacks` says how many of them fell back to
      the template, which is a per-run count and is described that way rather than as an attribution
      of any one line
      (`groq_fallbacks`), which is a true sentence either way and does not depend on the model
      answering;
   3. a second run shows `sent 0` and the `reminder_log` count line of `python -m chase status`
      shows zero new rows;
   4. `demo-reply` is timed against the deal flipping to Replied in HubSpot (the PATCH of section
      4.6), with the mechanism named aloud;
   5. the owner drags a deal to Won in HubSpot and the very next run does not chase it: the direct
      read of section 4.5 step 7 writes a `stage:won` stop, the claim is refused, and the report
      shows `stopped 1`. This is the "your robot will not embarrass me after a phone call" beat, and
      the code has always done it, it was simply never filmed;
   6. the Friday digest arrives on the owner's phone through n8n and is read aloud in its own words,
      "N quotes unanswered, AUD <total> sitting in them", followed by the failure beat: with
      `CHASE_SMTP_PORT` pointed at a closed port and Mailpit's API still answering, the run through
      n8n reports a real `status: failed` with a real `failed` count, and the alert arrives on the
      same phone.
3. The repository is public with a credential-free history: `.env` never committed, no token or
   key in any file or commit message.
4. The README's failure table (section 7) matches observed behaviour, not intended behaviour.
5. The README uses only the claims wording of section 1 and carries the seeded-versus-measured
   table of section 8.

## 14. Residual risks

| Risk | Why it stays | What limits it |
| --- | --- | --- |
| Custom-property search filtering fails | unverified in vendor text | spike; fallback is a stage-only saved view, one number weaker on camera |
| A rung SMTP refused is retried only while no higher rung has come due | the ladder sends the highest eligible step and never backfills | the retry is the common case (runs are 30 minutes apart, rungs are days apart); the failure table names the exception |
| Groq lines flip to templates mid-run | reasoning tokens versus 8,000 TPM unverified | `groq_fallbacks` shows it; the beat still works with templates |
| A reply lands between the run-start poll and a claim, or while Python is down | inherent to claim-then-send plus a webhook that never retries | one extra chase at most; the next run's poll stops it; named in the README, not engineered away |
| A deal dragged to Won mid-run is chased once more | the stage path is polled | named in the README |
| An n8n 3.0 upgrade on his existing instance changes credential defaults and rotates the API key | it is his live instance, not a container this repo owns | the running image's digest is recorded in the README at push time; runbook line; expected inside the six-month re-record window |
| A reply to the original quote email is never seen | the ladder only watches `quotes+<deal_id>` | named in the failure table and the honesty rules; in production the quote email carries the plus-address `Reply-To`, otherwise the first stop is manual |
| `host.docker.internal` misbehaves on his machine | it is the only path from Mailpit and n8n to Python | fails loudly (hook and schedule both error) but blocks evening 2 |
| The offset clock reads as staged to a non-technical viewer | inherent to a demo of a multi-day process | the on-camera sentence and the seeded-versus-measured table |
| Evening 3 carries the wrapper and the docs | the build is three sittings | the recording moved to its own fourth sitting, so an overrun costs a day rather than the Loom |
| The honest wording is a softer sales line than "exactly-once" and "cannot race" | it is the true wording | the Loom is scripted around the measured numbers, and that script does not exist yet |

## 15. Deferred options

Not built, not promised, written down so a reader knows they were seen:

| Option | What it would take | Why not now |
| --- | --- | --- |
| Two clients live in one portal | a third custom property `client` (3 of 10) as the discriminator, one search filter per client, the seed tagging each deal | the second client already proves "onboarding is a file" through tests and a diff at zero Loom seconds |
| The Friday digest as a HubSpot note or task on each unanswered deal | one engagement write per deal from `/digest` | Telegram reaches the owner's phone; a note on 20 deals is noise until someone asks for it |
| A `resolve` command for `unresolved` rows | a CLI that inspects Mailpit for the claimed row's `Message-ID` and marks it | the decision belongs to a human reading the mailbox; one SQL line in the runbook is enough at this size |
| Production reply adapter | an n8n IMAP node over the business's real mailbox posting the same fields to `/stop`, plus-address routing configured on that mailbox | documented-not-demoed; the poll in section 4.6 is already its shape |
| A `client` argument on `/run-ladder` and the n8n schedule per client timezone | one query parameter and a second schedule node | one live client today |
| Per-run `unmatched_replies` as a delta instead of a state count | Mailpit `after:` filter keyed on the previous run's time, which needs a third table or a settings row | two tables is the budget; the state count is honest and visible |
| Dollar totals from HubSpot's own filtered views | Sales Hub Starter | $0 forever |
| Reply detection by sender email for customers with several open quotes | a policy for the ambiguous case | surfaced as `unmatched_replies` today rather than guessed |
