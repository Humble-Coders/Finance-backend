# Handoff — ticket #56

**Ticket:** [#56](https://github.com/Humble-Coders/Finance-backend/issues/56) — [M4] Generate a monthly budget from real spending

## Summary

`GET /budgets/{YYYY-MM}` returns a month's budget built from the household's own spending:

- **Each category** gets the median of its debits over the last 3 complete months. A month with no spending counts as zero, so a one-off gets no line. The amount is rounded up to a whole unit.
- **Debt** is the larger of the observed payments and the sum of the debts' minimum payments.
- **Savings** is whatever expected income has left. When nothing is left there is no savings line, and the budget reports a `shortfall` instead.
- **`spent`** is counted with the dashboard's own rule (`countable`), so a line can never disagree with the dashboard figure beside it.

Until the household has 1 complete month and 20 countable transactions, the endpoint answers `learning` with progress toward both. That check is `learning_state()` in a new `app/services/learning.py`, so 4.2 and 4.3 can reuse it. While learning nothing is generated, but lines the user set by hand are kept and shown (product decision on review, 2026-10-06).

A month still running is regenerated on every read: lines the user set keep their amount, and only their suggestion moves. A month that has ended keeps the budget it had once that budget was built from some history, and it keeps the income it was built against. A past month read before its statements were imported fills in once they are.

Two new endpoints edit lines:
- `PUT …/lines/{category_id}` sets a line by hand.
- `DELETE …/lines/{category_id}/override` puts it back to its suggestion.

All three routes are gated by a new `auto_budget` feature, seeded enabled for everyone.

## Files changed

| File | Why |
|---|---|
| `alembic/versions/e4b8a2c61f93_budget_line_suggestion.py` | Adds `budget_line.suggested_minor_units` (BIGINT NOT NULL, default 0) and `is_user_set` (bool NOT NULL, default false), and `budget.has_history` (bool NOT NULL, default false) and `budget.expected_income_minor_units` (BIGINT NOT NULL, default 0). Defaults rather than a backfill, because no budget row exists yet. Purely additive. Follows on from #60's `b7d2e19c4a51`. |
| `alembic/versions/f1c7d93a5e28_seed_auto_budget_feature.py` | The `auto_budget` row: global (country and plan NULL) and enabled. Its id uses the same `uuid5` scheme as `b56e4dda349a`, it uses `ON CONFLICT DO NOTHING`, and the downgrade deletes it by id. |
| `app/models/planning.py` | The two new columns on `BudgetLine` and the two on `Budget`. `Budget.is_user_modified` now means "any line is user-set", and its comment says so. |
| `app/core/money.py` | New `round_up_to_whole_unit(minor_units, currency)`: a ceiling to the next whole unit. It accepts an `int` or a `Decimal`, because a median of two months can land on half a cent. It is the only rounding the budget does. |
| `app/services/dashboard.py` | Renamed `_countable` to `countable` so the budget and learning modules can import it. The behaviour is unchanged; all of this file's call sites were renamed. |
| `app/services/learning.py` (new) | `learning_state(session, household_id, currency, today)` returns `complete_months`, `transactions`, `needs` and `ready`, all counted by `countable` in one query. A month counts as complete once it has ended; the month that `today` falls in never counts. |
| `app/services/budget.py` (new) | The pure `suggest()`, `median()` and `savings_and_shortfall()`, plus the persistence: `budget_for`, `set_line` and `reset_line`. The budget row is created with `INSERT … ON CONFLICT DO NOTHING` and then locked `FOR UPDATE`, so two reads at once can't both generate lines. `_rebalance_savings` runs after every change once the household is ready. Every entry point takes `ready`: while learning nothing is generated, and a `GET` writes nothing. |
| `app/schemas/budget.py` (new) | `BudgetOut`, `BudgetLineOut`, `LearningOut` and `BudgetLineIn`. Every amount is a decimal string. |
| `app/api/budgets.py` (new) | The three routes, with `require_feature("auto_budget")` on the router. All three check `learning_state` and answer in the same shape, with `status` and, while learning, `learning`. Month parsing reuses `parse_month`; errors are `invalid_month`, `month_in_future`, `invalid_amount` (including anything above the BIGINT limit), `not_budgetable` and `not_found`. |
| `app/main.py` | Registers the router. |
| `tests/test_budget_suggest.py` (new) | 22 pure tests of `suggest` and its helpers, with no database. |
| `tests/test_budgets_api.py` (new) | 31 API tests, all marked `integration`, with "today" pinned to 2026-09-15. |
| `tests/test_money.py` | 10 tests for `round_up_to_whole_unit`. |

## How to test

1. Start a fresh local database:
   ```
   docker run -d --name finai-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 \
     ghcr.io/pgmq/pg17-pgmq:v1.5.1@sha256:e6f893a793751ed30c89f5f88e95aa52b77c1a03440b7d118a996866489ac0c6
   ```
2. Check the migrations apply, reverse and reapply:
   ```
   DATABASE_URL=postgresql://postgres:postgres@localhost:55432/postgres \
     PG_CONTAINER=finai-pg PYTHON=.venv/bin/python scripts/check_migrations.sh
   ```
   It should end with `All migration checks passed`.
3. Run the database tests:
   ```
   DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres \
     MIGRATION_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres \
     pytest -q -m integration
   ```
   Expect 453 passed (422 on `main`).
4. Run the rest with the same two variables: `pytest -q -m 'not integration'`. Expect 314 passed (282 on `main`).
5. Run `ruff check .` and `ruff format --check .`.
6. To try the endpoint by hand, read `GET /budgets/<current month>` for a household that has at least 20 transactions and one finished month.

## Acceptance criteria

| Criterion | Status |
|---|---|
| `suggest` is table-tested with no database: medians of 3, 2 and 1 months; one-offs; rounding at an exact unit and at .01 above; the debt line including no debts and minimums with no payments; savings; shortfall; zero income | ✅ Met (`tests/test_budget_suggest.py`) |
| A line's `spent` equals the dashboard's count; `total_spent` + `uncategorised_spent` + non-budgeted debits = `/dashboard`'s `expenses.actual` | ✅ Met (`test_spent_is_the_dashboard_s_count`, which checks each line against `countable` and the totals against `/dashboard`) |
| A suspected duplicate and a row in another currency are excluded from both suggestions and `spent` | ✅ Met |
| Learning: 19 transactions → not ready; a single partial month → not ready; 1 complete month and 20 transactions → ready | ✅ Met. Also tested: rows `countable` excludes don't count toward the threshold; a learning `GET` writes nothing; lines set by hand show while learning; and a line set on a past month while learning is kept once that month is generated. |
| A user's line survives regeneration: `allocated` stays the user's amount and `suggested` moves | ✅ Met |
| Reset restores the current suggestion; a hand-added line with no history is removed | ✅ Met. Resetting it again answers 404. |
| A month that has ended keeps its budget when later imports change its inputs | ✅ Met. Also tested: a past month read before its history fills in once imported, and keeps its income when the wizard changes. |
| `income` and `transfers` are never lines, whether generated or set (422) | ✅ Met |
| With `auto_budget` disabled, every route answers `403 feature_unavailable` | ✅ Met |
| One household can never read or write another's budget (404, never 403) | ✅ Met. Two households past the threshold read the same month and each sees only its own lines. |
| No float anywhere; amounts round-trip exactly as decimal strings | ✅ Met. There is no `float` in the diff, and `"123.45"` round-trips. |
| Both migrations apply, reverse and reapply in CI's `database` job | ✅ Passed locally with `check_migrations.sh` and in CI |
| `ruff check`, `ruff format --check` and `pytest` pass; the `database` job's pass count grew | ✅ All pass locally, and the database tests grew from 422 to 453. Confirm CI's `database` job shows 453. |

## Deviations / decisions

- **Rounding goes through a new money helper.** The ticket says to round up through `to_minor_units(…, allow_rounding=True)`, but that rounds half-up, so $120.01 would become $120. The acceptance criterion needs $121. I added `round_up_to_whole_unit` to `app/core/money.py`, which keeps rounding inside the money module.
- **`suggest` takes `CategoryRef(id, slug)` keys** rather than bare ids. That lets it decide what is never a line (`income`, `transfers`), what folds into debt, and what is savings, all with no database, so those rules are covered by the table tests.
- **"Only where data exists"** means the window drops months before the household's first countable transaction. Inside the window, an empty month still counts as zero.
- **`spent` and the history count debits only.** A refund doesn't reduce a line's spending, which matches `expenses.actual`.
- **Savings follows the user's choices.** It is recomputed from the final amounts, including lines the user set, after every read and edit, so raising a line lowers savings or opens a shortfall straight away. When the lines exactly equal income there is no savings line and `shortfall` is null.
- **The debt line:** the observed median is rounded up; the sum of minimum payments is used as entered.
- **A household's own `savings` or `debt_payment` category** feeds the single system line, matching how the dashboard folds them into one figure.
- **Missing income:** income that isn't set, or is recorded in another currency, counts as 0.
- **Edits always work.** `PUT` and `DELETE` work while the household is still learning, and on months that have ended ("manual budgeting is always available"). While learning they answer `learning` with the user's own lines, and no savings line is worked out around them. Editing a finished month rebalances its savings against the income it was built with, but doesn't regenerate its other lines. `PUT` accepts `0`, and refuses anything above the BIGINT limit with `422 invalid_amount`.
- **When a finished month freezes.** The ticket says a finished month "keeps the budget it had". It freezes only once a generation saw at least one month of history (`budget.has_history`); before that it keeps regenerating. Otherwise a month read before its statements were imported would stay empty for good.
- **`DELETE` with no line answers 404** and rolls back, so it leaves no empty budget row behind.
- **The review fixes edited `e4b8a2c61f93` in place** rather than adding a third migration. This is safe because it has only ever run on throwaway databases: production is on `b7d2e19c4a51`.
- **Reads write once ready.** For a household past the threshold, `GET` creates or updates the budget row, as `current_household` already does on every request. While learning, `GET` writes nothing.

## Open questions / follow-ups

- **Production must be migrated by hand after this merges.** Render never runs Alembic. Production is on `b7d2e19c4a51` and needs `e4b8a2c61f93` and `f1c7d93a5e28`; until then, every `/budgets` route answers `403 feature_unavailable`, because the feature row doesn't exist yet. The first migration only adds columns, so it is safe to run before or after the deploy.
- **The whole suite fails when run in one go locally.** Running `pytest -q` against the local database fails 166 tests with "attached to a different loop", on `main` as well. Running `-m integration` and `-m 'not integration'` separately, as CI does, passes.
