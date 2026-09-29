# Handoff — ticket #37

**Ticket:** [#37](https://github.com/Humble-Coders/Finance-backend/issues/37) — [M3] Review queue and correction learning

## Summary

Ticket 3.3 writes extracted transactions and flags the doubtful ones with
`needs_review` and a `review_reason`. Nothing read that flag. This ticket builds
the half that hands those rows to a person and acts on the answer.

**The queue:** list what is waiting (`GET /transactions/review`), correct a row
(`PATCH`), accept rows as they are (one, or many at once), or delete a row that
was never a transaction. An edit that would make a row a copy of one already
recorded is refused with the row it would duplicate named.

**The learning:** changing a row's category records one rule per merchant per
household. On every later import the rule is **applied exactly** — a row with
that merchant takes that category without the model being asked — and it is
applied straight away to that household's other rows still waiting for the same
merchant. Rows the user already confirmed are never moved. A rule into a
*shared* category is also shown to the model as an example, so near-variants of
the merchant benefit; a rule into the household's own category never is.

**Categories:** a household can create its own (`POST /categories`) and list
every one it can file into (`GET /categories`). No request can create a system
category.

**Also in this PR:** two test-suite guards — one refuses a remote database,
one refuses a database test CI would never run — and production migrated to head — it had been one migration
behind the code already deployed (see *Production*).

## Files changed

**New**

- `app/api/transactions.py` — the queue: list, correct, confirm (single and bulk), delete.
- `app/api/categories.py` — create and list household categories.
- `app/services/review.py` — the keyset cursor, and the one definition of "this import is finished".
- `app/services/corrections.py` — recording a correction as a rule and applying it to the queue.
- `app/services/categories.py` — turning a category name into a slug.
- `app/schemas/transactions.py`, `app/schemas/categories.py` — request and response shapes.
- `alembic/versions/a3f91c2e77b4_one_correction_per_merchant.py` — unique index on `category_correction (household_id, merchant_pattern)`.
- `tests/test_transactions_review.py`, `tests/test_corrections.py`, `tests/test_categories.py`.

**Changed**

- `app/services/ledger.py` — adds `collides_with`, the exact dedup-key check an edit is held to.
- `app/api/statements.py` — the confirmed-at decision moved to `review.stamp_if_finished` so both paths share it.
- `app/schemas/statements.py` — the amount-sign and date-range rules moved to module functions so the PATCH schema shares them.
- `app/models/categorization.py` — the model carries the new unique index.
- `app/main.py` — registers the two routers.
- `tests/conftest.py` — refuses a non-local database unless `ALLOW_REMOTE_TEST_DB=1`;
  and refuses to collect any test marked `requires_db` without `integration` (see below).
- `CLAUDE.md` — how to run the suite against the local container.

## How to test

The suite now refuses to run against `.env`'s database, which is production.
Start the local container once:

```
docker run -d --name finai-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 \
  ghcr.io/pgmq/pg17-pgmq:v1.5.1@sha256:e6f893a793751ed30c89f5f88e95aa52b77c1a03440b7d118a996866489ac0c6
```

Then:

```
export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres
export MIGRATION_DATABASE_URL=$DATABASE_URL
alembic upgrade head
pytest tests/test_transactions_review.py tests/test_corrections.py tests/test_categories.py
ruff check . && ruff format --check .
```

Migration rehearsal on the same container:

```
alembic downgrade -1 && alembic upgrade head
```

**Running the whole suite locally shows 21 failures in
`tests/test_statements_endpoint.py`. They are not from this branch.** The same
21 fail on a clean checkout of `main` against the local container, and `main` is
green on CI. They are an asyncpg event-loop error (`Future attached to a
different loop`) that appears only when the full suite runs against the Colima
container. Per-file runs and CI are the reliable signals locally.

## Acceptance criteria

| Criterion | Status | Held by |
|---|---|---|
| Review list returns only this household's rows needing review, each with its reason; paging never skips or repeats | **met** | `TestWhatIsInTheQueue`, `TestHouseholdIsolation`, `TestPaging` |
| A category correction writes exactly one `category_correction`; correcting the same merchant again updates it | **met** | `test_a_correction_writes_exactly_one_rule`, `test_correcting_the_same_merchant_again_replaces_the_rule` |
| Other unreviewed rows for the merchant take the new category; confirmed rows untouched — one of each | **met** | `test_waiting_rows_move_and_confirmed_rows_do_not` |
| An amount or date change re-runs dedup and says so rather than writing a duplicate | **met** | `TestAnEditThatWouldDuplicate` — 409 `would_duplicate` naming the existing row |
| Bulk confirm takes a list, is idempotent, and one foreign row fails the whole request | **met** | `test_one_foreign_id_anywhere_confirms_nothing` (first / middle / last), `test_a_retry_is_harmless_and_reports_nothing_new` |
| `POST /categories` cannot create a system category however shaped, nor duplicate a slug (409, logged) | **met** | `TestNeverASystemCategory`, `test_the_same_name_twice_is_a_logged_409` |
| An import whose last review resolves gets `confirmed_at`; one with a row outstanding does not | **met** | `TestFinishingAnImport` (PATCH), and the finished / not-finished pair for bulk confirm and for delete |
| Corrections never cross households, in reads or prompts | **met** | `TestNeverAcrossHouseholds` — rows not moved, and `_examples()` for the other household does not contain the rule |
| `ruff check`, `ruff format --check`, `pytest` pass; migrations apply, reverse, re-apply; CI green | **met locally** | CI's first green run did **not** cover these tests — see below |

## Deviations & decisions

**`GET /categories` was added, though the ticket asked only for `POST`.**
`PATCH /transactions/{id}` takes a category id and nothing returned one, so the
review screen could not offer a picker. Agreed with the manager before building.

**Rows a rule moves stay in the queue.** They take the category, but nobody has
looked at them — a low-confidence row can still have the wrong amount.

**An edit is held to the exact dedup key, not import's near-match rule.** Import
*flags* a same-day, same-amount row with a different description for a person to
compare. Refusing an edit on those grounds would make ordinary corrections
impossible: two $20 withdrawals in one afternoon are two withdrawals.
`test_a_same_day_same_amount_row_with_another_description_is_allowed` holds it.

**Answering a row drops its `duplicate_of_id`.** The pointer is evidence for an
open question; once the row is kept, the answer was "no".

**Merchant rules match on lower case and single spaces, nothing looser.**
"Cafe Luna" and "cafe  luna" are one merchant. "Spotify P3a4b5c6" and
"Spotify Q9r8s7t6" are two — see *Follow-ups*.

**Rules are applied exactly at import, before the model is asked** (review
round 3, the manager's option (a)). Before this, a correction reached later
imports only as an example in the prompt, and a rule into a household's own
category could never work: the prompt said `"etsy" is side_business`, but the
categorizer accepts only shared-category answers, so the model's
`side_business` was thrown away and every month's Etsy charge went back to
review — while the household's category name was still sent to the provider.
Now a matching row is filed by the rule and never sent; the prompt shows only
rules into shared categories; and rules still apply when the model is
unreachable. A rule answers the category only — a row flagged for a doubtful
amount keeps that flag. This reads PRD §4.5's "prompt-side, never training" as
permitting an exact lookup, which trains nothing; that interpretation was the
manager's call.

This changed ticket 3.3's `test_a_household_correction_steers_the_answer`, which
asserted the correction reached the prompt for an *exact* match. It no longer
does, by design, so the test now asserts the rule filed the row and the model
was not asked; a new sibling test keeps the prompt path covered for a merchant
the rule does *not* match exactly.

**Bulk confirm takes its row locks in id order**, so two overlapping requests
cannot each hold a row the other waits for — Postgres would otherwise fail one
with a 500. This is **not covered by a test**: a deadlock needs two concurrent
transactions, which this suite does not simulate.

**There is one definition of "the same merchant", in Python.** Review found a
second, in SQL, used to pick which rows a rule reaches. It disagreed with the
first: Postgres `trim()` strips only spaces and its `\s` misses a non-breaking
space, so a merchant with a leading tab or a U+00A0 was one merchant to the rule
and another to the rows. Candidates are now read and matched with
`merchant_key` itself, and a test puts a waiting row through every one of the
29 characters `str.split()` treats as whitespace. A merchant typed through
`PATCH` is stored in the same single-spaced form import writes, and a blank one
is refused. The mismatch could not fire yet — import only writes canonical
merchants and a typed one always resolves its row — but it would have the day
anything else writes a merchant onto a waiting row.

**Agreeing with the categorizer records nothing.** Setting a row to the category
it already has, or confirming it, is not a correction.

**Category slugs use underscores**, matching the seeded taxonomy
(`debt_payment`). This is load-bearing: the system-clash check compares slugs,
so "Debt payment" must become `debt_payment` to be recognised as the category
that exists. A clash with a system category is refused even though the database
would allow it, and the 409 names the existing category so the client can use
it. Unknown request fields are refused rather than ignored.

**The bulk-confirm 404 does not say which id failed**, since that would reveal
which guessed ids exist in another household. Everywhere, another household's
row or category is answered exactly like one that does not exist — never 403.

**A grouped amount (`"1,234.50"`) is refused.** Accepting it means deciding what
`"1.234,50"` means, a locale guess the server should not make. The client sends
a plain decimal.

**Rule timestamps use `clock_timestamp()`, not `now()`.** The categorizer shows
only the most recently updated rules. An upsert does not fire the ORM's
`onupdate`, and `now()` is fixed for a whole transaction, so rules written in
one would tie.

## The first CI run did not test this ticket

CI's first run on this branch was green, and it had not run a single one of this
ticket's database tests. The database job runs `pytest -m integration`; the new
test files were marked `requires_db` but not `integration`, so that job
deselected all 84 of them, and the fast job — which has no database — skipped
them. The run reported `200 passed` either way.

It was caught by checking the count rather than the tick: 200 is the same number
the database job reported before this branch existed. The files now carry the
marker, and `conftest.py` refuses to collect any test that needs a database but
is not `integration`, so the mistake fails loudly instead of passing silently.
The database job reported **284 passed** — 200 plus these 84.

## Production

Production was at `b2d5f8a13c47` — one migration behind `main`, missing ticket
3.3's `occurrence`, `review_reason`, `duplicate_of_id` and the `review_reason`
enum, all of which 3.3's merged code writes. Production had 0 transaction rows.

Both pending migrations were applied on 2026-09-28, with the manager's
authorisation, after rehearsing apply / reverse / re-apply on the local
container and confirming `category_correction` was empty. Production is at
`a3f91c2e77b4`, and the columns, enum and index were checked read-only
afterwards. **This PR's migration is therefore already live**; merging applies
no schema change.

**Until this merges, production is on a revision `main` does not have.** The live
app is unaffected — Render runs no migrations on deploy (`render.yaml:18-19`)
and its health check, `/healthz`, does not touch the database. But:

- any `alembic` command run from `main` against production fails with "Can't
  locate revision a3f91c2e77b4";
- if another PR adds a migration on top of `c7e1a93b4d82` and merges first,
  production ends up on a sibling branch and the history has two heads.

**Merge this before any other PR that adds a migration.**

## What the tests actually hold up

Each behaviour above was checked by deliberately breaking the code and
confirming that exactly the named test fails, and no others. Across the ticket:

- **Paging** — cursor on the date alone fails `test_every_row_is_seen_exactly_once`;
  an offset cursor fails `test_a_row_resolved_mid_walk_does_not_shift_the_rest`.
  Both tests are needed: offset paging still passes the first.
- **Isolation** — dropping the household filter from the queue, the duplicate
  lookup, PATCH, bulk confirm, delete, the category check, the category list,
  the rule update or the categorizer's prompt query each fails its own test.
- **Correction** — removing the upsert crashes the second correction with a
  unique violation; removing the timestamp fails the prompt-order test.
- **Deletion** — sweeping away a suspected duplicate or a learned rule with the
  deleted row each fails a test.

## Follow-ups

1. **Bare reference codes defeat merchant rules — completely, for a household's
   own categories.** `normalization.merchant()` strips codes marked `#`, `*` or
   `•` but keeps bare ones, so `SPOTIFY P3A4B5C6` becomes `Spotify P3a4b5c6`, and
   a rule matches exact merchants only. If a merchant's code changes each month,
   correcting one charge does not file the next. What happens then depends on
   the category the rule points at:

   - **Shared category:** the rule is shown to the model as an example, so it
     *may* still generalise to the new code. Not guaranteed.
   - **Household category:** nothing helps. By design these rules are never
     shown to the model, and the model could not answer with a household
     category anyway, so the charge goes to review every month.

   So for anyone using their own categories on subscriptions, this is the
   difference between a correction working once and working at all. Fixing it
   changes merchant names everywhere, so it needs its own ticket; given the
   above, it is worth scheduling ahead of 6.2 rather than waiting for it.
2. **No merge was built, though ticket 3.3 expected one here.**
   `ledger.may_merge_onto` was written by 3.3 "for 3.4, which owns the merge
   itself", but ticket 37 never asked for a merge and this PR does not build
   one. The helper has no callers; its docstring now says so instead of claiming
   3.4 uses it.

   A merge would answer a suspected duplicate with "yes, same transaction":
   move the better description from a re-import onto the older row — only if
   the user has not touched that row yet, which is the rule `may_merge_onto`
   states — and remove the newer one, keeping the older row and its history.
   Without it, the user deletes one of the two rows: keeping the old one keeps
   the worse description, and keeping the new one loses any category they had
   set on the old one. **Needs a decision on whether it gets its own ticket,
   and 3.7's duplicate screen should know which way it went** — today it can
   offer only "delete this one" or "delete that one".
3. **For 3.7 — a suspected duplicate can lose its match.** Deleting the row a
   suspect was compared against leaves the suspect in the queue as
   `suspected_duplicate` with `duplicate_of: null`. There is no better reason to
   give it, so the screen should treat that as "nothing left to compare".
4. **The 21 local full-suite failures** are worth their own look. They make the
   local full run useless as a signal.
