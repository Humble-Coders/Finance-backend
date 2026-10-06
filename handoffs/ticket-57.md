# Handoff — ticket #57

**Ticket:** [#57](https://github.com/Humble-Coders/Finance-backend/issues/57) — [M4] Compute the Money Health Score and keep its history

## Summary

`GET /health-score` returns the Money Health Score: one number from 0 to 100, what it is made of, and the last 12 daily snapshots. Formula v1 has three parts:

- **`savings_consistency` (40):** each of the last 3 complete months' savings rate, from 0 to 20%, scaled so 20% scores 100.
- **`spending_vs_budget` (35):** last complete month's spending lines against their allocations, weighted by allocation.
- **`debt_payments` (25):** last complete month's debt payments against the debts' minimum payments. No debts scores 100.

A part that can't be scored is left out and the weights are renormalised over the rest. With nothing scorable there is no score.

The arithmetic is a pure `score()` function: `Decimal` throughout, no database and no model call, so the same inputs always give the same score. `current_score()` gathers the inputs:
- the dashboard's per-month figures, counted by `countable`;
- last month's budget through 4.1's `budget_for`, so it scores the exact lines the budget screen shows;
- the setup's debts.

While the household is still learning (`learning_state`), nothing is scored or written.

There is no scheduler. The score is computed when read, and today's snapshot (UTC) is upserted; a past day's is never rewritten. Each snapshot stores its formula version and its exact inputs, which reproduce its score. The route is gated by a new `health_score` feature, seeded enabled for everyone.

## Files changed

| File | Why |
|---|---|
| `alembic/versions/a9d3e6b14c72_seed_health_score_feature.py` | The `health_score` feature row: global and enabled, with the same `uuid5` id scheme as `auto_budget`. Uses `ON CONFLICT DO NOTHING`; the downgrade deletes it by id. Follows on from `f1c7d93a5e28`. No schema change: `health_score_snapshot` already exists. |
| `app/services/health_score.py` (new) | **Formula:** `FORMULA_VERSION`, `WEIGHTS`, `SAVINGS_RATE_CAP` and the window and history lengths, under a comment saying any change means bumping the version. **Arithmetic:** input dataclasses (`MonthFlow`, `BudgetLineUse`, `DebtPicture`, `ScoreInputs`); the three component functions; `score()`; and `to_json` / `inputs_from_json` for the stored breakdown. **Persistence:** `current_score()` plus `_keep()`, which upserts on `(household_id, scored_on)`. |
| `app/services/dashboard.py` | Renames `_figures_by_month` to `figures_by_month` so the score can import it. The behaviour is unchanged. |
| `app/schemas/health_score.py` (new) | `HealthScoreOut`, `ComponentOut` and `SnapshotOut`. Reuses 4.1's `LearningOut`. |
| `app/api/health_score.py` (new) | `GET /health-score` with `require_feature("health_score")`. Builds each component's `inputs` with money as decimal strings and the renormalised `weight` to two places. |
| `app/main.py` | Registers the router. |
| `tests/test_health_score.py` (new) | 34 tests, no database. |
| `tests/test_health_score_api.py` (new) | 10 tests marked `integration`, with "today" pinned to 2026-09-15 (and moved for the history tests). |

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
3. Run the database tests:
   ```
   DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres \
     MIGRATION_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres \
     pytest -q -m integration
   ```
   Expect 465 passed (455 on `main`).
4. Run the rest with the same two variables: `pytest -q -m 'not integration'`. Expect 348 passed (314 on `main`).
5. Run `ruff check .` and `ruff format --check .`.

## Acceptance criteria

| Criterion | Status |
|---|---|
| `score` table-tested with no database: each component at 0 and 100; the 20% cap (30% = 20%); a loss month scores 0; a month with no income is left out; overspend of exactly 100% scores 0, and beyond stays 0; no debts scores 100; debts with no minimums are unavailable; renormalisation; nothing available gives no score; half-up at `.5` | ✅ Met (`tests/test_health_score.py`) |
| The same inputs always give the same score, including reordered inputs | ✅ Met (`TestDeterminism`) |
| History is never rewritten: day 1 unchanged after a day-2 read; two reads on day 1 around an import update one row in place | ✅ Met (`TestHistory`) |
| Every snapshot stores `formula_version` and a breakdown that reproduces the stored score | ✅ Met. There is a pure test of `to_json` → `inputs_from_json` → `score`, and a database test of the stored row. |
| Before the threshold: `learning`, and no snapshot written | ✅ Met |
| `spending_vs_budget` reads the same budget lines 4.1 serves; setting a line by hand moves the component | ✅ Met (`test_it_reads_the_budget_4_1_serves`: a $300 line gives 50, and the score is 83) |
| `health_score` disabled → `403 feature_unavailable`; no household reads another's score or history | ✅ Met |
| No float and no LLM call, with a guard | ✅ Met. Two guards parse the module's own code: one fails on any import naming `llm`, `openai` or `httpx`, the other on any float literal or use of `float`. |
| The migration applies, reverses and reapplies; ruff and pytest green; the `database` job's count grew | ⏳ All pass locally, and the database tests grew from 455 to 465. Confirm in CI. |

## Deviations / decisions

- **What a component shows:** `score` is a whole number for display; the overall score is computed from the exact values. The two can differ by one from a weighted average of the displayed parts, which a test pins. `weight` is the renormalised share as a decimal string, for example `"61.54"`, and `"0.00"` when unavailable.
- **Nothing scorable:** the response is `status: "ready"` with `score: null`, every component unavailable, and **no snapshot**, because `score` is NOT NULL and there is nothing to keep.
- **`spending_vs_budget`** counts as unavailable when its lines have no allocation (all $0), since there is nothing to weight.
- **`debt_payments`:**
  - `required` is the sum of the minimums that were given. With some debts lacking one, the others still count.
  - Minimums that add up to $0 score 100.
  - `paid` is the dashboard's `debt_paid` for the month, which includes a household's own `debt_payment` category, the same as everywhere else.
- **Monthly income** is the month's countable credits, exactly the dashboard's `income.actual`, as the ticket says. That includes refunds and money coming in from transfers.
- **Reading the score writes last month's budget.** Reading the score calls `budget_for` on last month, so it creates or settles that month's budget, as opening it on the budget screen would.
- **`history`** is the last 12 snapshots, one per day the score was read, not 12 months.

## Open questions / follow-ups

- **Production needs three migrations run by hand:** this one (`a9d3e6b14c72`) and #61's two (`e4b8a2c61f93`, `f1c7d93a5e28`). Production is still on `b7d2e19c4a51`. Until they run, `/budgets` and `/health-score` answer `403 feature_unavailable`.
- **For the product owner:**
  - The weights and rules of v1 are the manager's 2026-10-04 decision, to be confirmed at `/review-ticket`.
  - Monthly income counts every credit, so a refund or transfer in lifts the savings rate a little. If income should be only `income`-category credits, that's a rule change and a bump to `v2`.
- **4.4 (the score card)** should label history points by day, since snapshots exist only for days the app was opened.
