# Chase Ladder

Quote follow-up for a small business that runs on HubSpot Free. One YAML file per client sets the
ladder, the sending hours and the templates; Python does every decision and is tested; n8n is the
scheduler and the notifier; SQLite is the send log; HubSpot is the only place quotes live.

## What it claims

This paragraph was written before any schema, and it is the ceiling on what this README and the
demo may say. Nothing below it is allowed to claim more.

> Every quote a business sends is chased on a schedule the business sets in one YAML file, and the
> chasing stops when the customer replies, when the deal is moved to Won or Lost, or when someone
> tells it to stop. Each quote and step gets **at most one successful send**; a send SMTP refused is
> marked failed and retried on a later run. Re-running the ladder adds zero rows. A crash between
> the ledger commit and the email send is reported as *unresolved*, with a count, and is never
> retried automatically. There is **no lost update between a stop and a claim**, because the run and
> the stop both write the same SQLite file under `BEGIN IMMEDIATE`; the Won/Lost path is a direct
> HubSpot read of each due deal before any claim, once per run, with an inherent window this
> project names. A Friday digest says **which quotes are still unanswered and how much is sitting
> in them**.

Read the wording literally. It is *at most one successful send*, not "exactly once": the email
leaves over SMTP, outside the SQLite transaction, so a send whose outcome is ambiguous (timeout,
connection reset) stays `claimed`, is never auto-retried, and surfaces in the run report as
`unresolved`. A send the server actively refused is a different case, marked `failed` and retryable.

## Status

In build. The offline core is on `main`: the clock, the config loader, the ladder arithmetic, the
SQLite claim transaction under `BEGIN IMMEDIATE`, the templates, the run loop and the CLI, with 145
tests passing. The HubSpot client, the Mailpit reply path, the HTTP routes, the Friday digest and
the n8n workflow are the next two sittings, and the claims above describe the finished system, not
today's tree.

## Design

The full design lives in
[`docs/superpowers/specs/2026-08-29-chase-ladder-design.md`](docs/superpowers/specs/2026-08-29-chase-ladder-design.md):
architecture, the data model, the run report, failure handling, the seeded-versus-measured rules,
a verified-facts table where every vendor limit carries a URL or the word unverified, and the
testing plan.

## Running the tests

```
pip install -r requirements.txt
python -m pytest -q
```

No credentials are needed for the suite, and nothing in this repository can authenticate to
anything. Copy `.env.example` to `.env` and fill it in by hand for the live paths.
