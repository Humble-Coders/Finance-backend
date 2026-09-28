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
household. It is shown to the categorizer on every later import and applied
straight away to that household's other rows still waiting for the same
merchant. Rows the user already confirmed are never moved.

**Categories:** a household can create its own (`POST /categories`) and list
every one it can file into (`GET /categories`). No request can create a system
category.

**Also in this PR:** a guard that refuses to run the test suite against a
remote database, and production migrated to head — it had been one migration
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
- `tests/conftest.py` — refuses a non-local database unless `ALLOW_REMOTE_TEST_DB=1`.
- `CLAUDE.md` — how to run the suite against the local container.

## How to test

The suite now refuses to run against `.env`'s database, which is production.
Start the local container once:

```
docker run -d --name finai-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 \
  ghcr.io/pgmq/pg17-pgmq:v1.5.1
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
| `ruff check`, `ruff format --check`, `pytest` pass; migrations apply, reverse, re-apply; CI green | **met locally** | CI result is on the PR |

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

1. **Bare reference codes defeat merchant rules.** `normalization.merchant()`
   strips codes marked `#`, `*` or `•` but keeps bare ones, so
   `SPOTIFY P3A4B5C6` becomes `Spotify P3a4b5c6`. If a merchant's code changes
   each month, correcting one charge will not move the next in the queue. The
   categorizer likely still generalises from the example, but that is not
   guaranteed. Fixing it changes merchant names everywhere, so it belongs in its
   own ticket or in 6.2.
2. **For 3.7 — a suspected duplicate can lose its match.** Deleting the row a
   suspect was compared against leaves the suspect in the queue as
   `suspected_duplicate` with `duplicate_of: null`. There is no better reason to
   give it, so the screen should treat that as "nothing left to compare".
3. **The 21 local full-suite failures** are worth their own look. They make the
   local full run useless as a signal.
