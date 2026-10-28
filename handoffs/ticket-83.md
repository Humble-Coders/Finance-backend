# Handoff — ticket #83

**Ticket:** #83 — [M6] Monthly totals and date-range data tools for the AI coach

## Summary

This ticket is the data layer for the AI coach (M6). It calls no model.

- **Monthly totals table.** A new `monthly_category_total` table holds each household's money summed by month, category and direction. It uses the dashboard's duplicate rule, keeps every currency apart, and holds one-offs both in the total and separately. Ten years of history becomes about a thousand small rows instead of roughly 36,000 transactions.
- **Kept current on every write.** `rollups.refresh` rebuilds only the months a ledger write touched, in the same transaction as the write. Each call bumps `ledger_state.ledger_version`, so caches built in 6.2 and 6.4 are invalidated without extra work.
- **Four data tools.** `app/services/chat_tools.py` adds `spending_summary`, `period_compare`, `transaction_search` and `top_merchants`. Each has a JSON-schema `ToolSpec` for #69's `run_tools`, and results are compact summaries with no ids.
- **Backfill.** `python -m app.scripts.rebuild_rollups` builds totals for existing households.
- **Test data.** A 10-year fixture of about 35,500 rows backs the tests.

## Files changed

**Schema**
- `alembic/versions/c4e9a2f7b815_monthly_totals_and_ledger_version.py` creates `monthly_category_total` and `ledger_state`.
  - Row level security is on for both.
  - A currency check is enforced.
  - Each table has a unique key: a `COALESCE` expression index for the totals, and `household_id` for the state.
  - The household foreign key cascades. The category foreign key is `RESTRICT`.
  - The migration reverses cleanly.
- `app/models/derived.py` and `app/models/__init__.py` add the `MonthlyCategoryTotal` and `LedgerState` models.

**Rollups**
- `app/services/rollups.py`:
  - `refresh(session, household_id, dates)`, `rebuild(session, household_id)` and `ledger_version(...)`;
  - the version upsert runs first and serialises refreshes of one household.
- `app/services/dashboard.py`: `not_an_unresolved_duplicate()` is pulled out of `countable`, so the totals and the dashboard share one duplicate rule.

**Write paths that now refresh the totals**
- `app/api/transactions.py`:
  - manual add, including dates re-filed by pairing;
  - `PATCH`: the old date, the new date, and dates re-filed by learning;
  - bulk confirm, single confirm, one-off and delete.
- `app/api/statements.py`: import commit. This covers the saved rows' dates, dates re-filed by pairing, and dates re-filed by rules learned at the import. `_learn_chosen` now also returns those dates.
- `app/api/budgets.py`: a commitment payment typed in.
- `app/services/corrections.py`: `Learned.recategorized_on`, the dates its rule re-filed, via `RETURNING`.
- `app/services/transfers.py`: `Paired.refiled_on`, the dates pairing re-filed.

**Tools**
- `app/services/chat_tools.py`:
  - the four tools;
  - the `TOOLS` specs;
  - `handlers_for(session, household_id, currency, today)`, which binds the household on the server side;
  - `data_range(...)`, for 6.4's prompt.

**Backfill**
- `app/scripts/__init__.py` and `app/scripts/rebuild_rollups.py`: rebuild every household, one transaction each, and print counts only.
- `.github/workflows/ci.yml`: a "Monthly totals rebuild script runs" step after the migration check.

**Tests**
- `tests/fixtures/long_history.py`: 10 years (2016-01 to 2025-12) for one household from a seeded generator. It includes pay cheques, rent, savings transfers, everyday spending, unfiled rows, USD rows, suspected duplicates and a one-off each January.
- `tests/test_rollups.py`: 16 tests, all `integration`.
- `tests/test_chat_tools.py`: 19 tests, all `integration`.

## How to test

These steps use the local Docker database only. Never point them at `.env`, which is production.

1. Point both URLs at the local container:
   ```bash
   export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres
   export MIGRATION_DATABASE_URL=$DATABASE_URL
   ```
2. Migrate the local container:
   ```bash
   .venv/bin/python -m alembic upgrade head
   ```
3. Run the new tests. Expect 35 passed:
   ```bash
   .venv/bin/pytest -q tests/test_rollups.py tests/test_chat_tools.py
   ```
4. Run both suites. Expect 521 without the database and 711 with it (676 before this ticket):
   ```bash
   .venv/bin/pytest -q -m "not integration"
   .venv/bin/pytest -q -m integration
   ```
5. Run the backfill:
   ```bash
   .venv/bin/python -m app.scripts.rebuild_rollups
   ```
6. Run the migration round trip. It needs an **empty** throwaway container, for example a fresh one on port 55432:
   ```bash
   DATABASE_URL=postgresql://postgres:postgres@localhost:55432/postgres PG_CONTAINER=<container> PYTHON=.venv/bin/python scripts/check_migrations.sh
   ```
7. Lint:
   ```bash
   .venv/bin/ruff check app tests && .venv/bin/ruff format --check app tests
   ```

## Acceptance criteria

| Criterion | Status | Where |
|---|---|---|
| Each month's rollup equals `/dashboard`'s figures to the minor unit | Met | `test_every_month_to_the_minor_unit`: all 120 months, income and expenses, via `/dashboard` |
| After every write path, the rollup equals a fresh `rebuild` and `ledger_version` went up | Met | `TestEveryWritePathKeepsThemCurrent`: manual add, date move, correction with rule re-filing, one-off, confirm, bulk confirm, delete, typed commitment payment, and import with pairing re-filing a row from an earlier month |
| Ten years: `spending_summary(2016-01, 2025-12, by year)` gives 10 rows equal to raw sums, in under 200 ms; `period_compare` returns server-computed differences | Met | `test_spending_by_year_is_ten_rows_equal_to_the_raw_sums` (asserts under 0.2 s; about 4 ms locally); `test_two_years_compared_by_the_server` |
| `transaction_search` over 10 years for a common merchant: at most 50 rows, true `total_count` and `total_amount`, `truncated: true` | Met | `test_a_common_merchant_over_ten_years_is_a_summary`. The total is split into `total_out` and `total_in` (see below) |
| Other-currency rows and unresolved duplicates are in no total; one-offs are in totals and reported apart | Met | `test_other_currencies_and_duplicates_are_in_no_total`, `test_one_offs_are_in_the_total_and_apart`; the tools filter to the household currency |
| No ids or account numbers; amounts are decimal strings; no float | Met | `test_no_ids_no_floats_money_as_decimal_strings`, which walks every tool's result |
| One household's tools never return another's data | Met | `test_one_households_tools_never_return_anothers`; `handlers_for` binds the household on the server, and no schema accepts a household argument (`TestTheSchemas`) |
| Migration applies, reverses and re-applies; the rebuild script runs in CI; ruff and pytest green; `database` count grew | Met locally | `check_migrations.sh` passed on a fresh container; CI step added; the database suite grew from 676 to 711. CI's own run is still to be checked on the PR |

## Deviations and decisions

- **The totals keep every currency; readers filter to the household's.** The ticket's table already keys on currency. Keeping all currencies means a country or currency change needs no rebuild, so the planned "currency change → full rebuild" path was dropped. The tools apply `countable`'s currency half when reading.
- **Unique key as a `COALESCE` expression index**, not `NULLS NOT DISTINCT`. It works on any Postgres version and folds the "not filed" rows (NULL category) into one row per month.
- **Category foreign key is `RESTRICT`.** No route deletes a category today. A future one will fail loudly until it rebuilds the household's totals, instead of leaving them filed wrongly.
- **Transfers are excluded, as on the dashboard.** `spending_summary`, `period_compare` and `top_merchants` leave out money moved between the household's own accounts (`NOT_A_FLOW`).
- **`transaction_search` lists every countable row**, transfers included, with each row's category. Its total is reported as `total_out` and `total_in`, because a search without a direction can match both.
- **Direction is `"out"` / `"in"`** in the tool arguments rather than `debit` / `credit`, which is clearer to a model. `spending_summary` and `period_compare` default to `"out"`.
- **Dates.** A range that *starts* in the future, or runs back to front, raises `ToolError`. A range that *ends* in the future is clamped to today, and the result reports the range it actually covered, so "this year" still works mid-year.
- **No new indexes.**
  - Locally on the fixture, every tool takes 4–32 ms.
  - `EXPLAIN` shows sequential scans only because the fixture household is the whole test table, and a 10-year query has to read all of that household's rows anyway.
  - A substring merchant match can't use a btree index; fuzzy matching is 6.6's `pg_trgm`.
  - The existing `ix_transaction_household_occurred` serves a household's range among many households.
- **Merchant rollups** (open question 1) aren't needed: `top_merchants` over 10 years takes about 26 ms.
- **Concurrency.** `refresh` upserts the version row first. Its row lock makes two refreshes of the same household take turns, so their delete-and-insert can't collide on the unique key.

## Open questions and follow-ups

- **Production, by hand, after merge, with approval:**
  1. Confirm the PR has merged.
  2. Run `alembic upgrade head` (migration `c4e9a2f7b815`).
  3. **Immediately** run `python -m app.scripts.rebuild_rollups`.

  Until the script runs, households that existed before the migration have totals only for months written since, and `ledger_version` 0.
- **For 6.4:** register `chat_tools.TOOLS` with `handlers_for(...)`. Use `data_range(...)` in the cached system prompt. Key the answer cache on `rollups.ledger_version(...)`.
- **For 6.2:** the suggested-question templates can read these same tools.
- **Any future ledger write path** must call `rollups.refresh` with every date it changes. The write-path tests show the pattern.
- **Import quota for long histories** remains a plan decision (7.1), out of scope here.
