# Handoff — ticket #58

**Ticket:** [#58](https://github.com/Humble-Coders/Finance-backend/issues/58) — [M4] Add the budget, the score and data freshness to the dashboard

## Summary

`GET /dashboard` gains five optional fields, so apps already in testers' hands decode it unchanged:

- **`spend_by_category`:** the month's countable debits per category, largest first, with uncategorised rows as one entry. One grouped query; the entries sum exactly to `expenses.actual`.
- **`budget`:** 4.1's `budget_for` for the month. Lines come in `/budgets` order, each with how far it is `over`, plus `savings`, `debt` and the totals.
- **`health_score`:**
  - **Current month:** 4.2's `current_score`, held score and notice included.
  - **Past month:** the last snapshot on or before that month's end, read but never computed.
  - **`previous_score`:** the last snapshot in the month before.
- **`as_of`:** the newest countable transaction and the newest import that saved rows, across the whole household.
- **`learning`:** one still-learning state for the budget and the score together.

The budget appears only for households with `auto_budget`, and the score only with `health_score`. Neither is produced for a month that hasn't begun. No cache table, as the manager decided: the dashboard stays live SQL.

## Files changed

| File | Why |
|---|---|
| `app/services/dashboard.py` | `spend_by_category()`: one grouped query over `countable` debits, joined to `Category`. `as_of()`: one statement with two scalar subqueries, for the newest countable `occurred_on` and the newest `StatementImport.created_at` with at least one transaction. Both have result dataclasses. |
| `app/api/dashboard.py` | Puts the new fields together in the router. `budget.py` and `health_score.py` already import the dashboard service, so composing there would be circular. Adds `_today()`, which `_month_or_now` now uses, and passes `today` to `build`. Feature checks use `capabilities.resolve`. Adds `_budget_out`, `_score_out` and `_last_snapshot`. Commits at the end, because reading the current month keeps today's score snapshot and settles the month's budget. |
| `app/api/health_score.py` | `_missing_line` becomes `missing_line`, so the dashboard's notice reads exactly like `GET /health-score`'s. No behaviour change. |
| `app/schemas/dashboard.py` | `CategorySpendOut`, `DashboardBudgetLineOut`, `DashboardBudgetOut`, `DashboardScoreOut` and `AsOfOut`. On `DashboardOut`, `spend_by_category`, `budget`, `health_score`, `as_of` and `learning`, all defaulted. Reuses `LearningOut` and `NoticeOut`. |
| `tests/test_dashboard_additions.py` (new) | 12 tests marked `integration`, with every clock pinned to 2026-09-15 (and moved for the score cases). |

## How to test

1. Start a fresh local database:
   ```
   docker run -d --name finai-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 \
     ghcr.io/pgmq/pg17-pgmq:v1.5.1@sha256:e6f893a793751ed30c89f5f88e95aa52b77c1a03440b7d118a996866489ac0c6
   ```
2. Bring it to head: `MIGRATION_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres alembic upgrade head`. This ticket has no migration of its own.
3. Run the database tests:
   ```
   DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres \
     MIGRATION_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres \
     pytest -q -m integration
   ```
   Expect 482 passed (470 on `main`).
4. Run the rest with the same two variables: `pytest -q -m 'not integration'`. Expect 349, unchanged.
5. Run `ruff check .` and `ruff format --check .`.

## Acceptance criteria

| Criterion | Status |
|---|---|
| `spend_by_category` sums to `expenses.actual`, across several categories, an uncategorised row, a suspected duplicate and a row in another currency | ✅ Met (`test_it_sums_to_expenses_exactly`) |
| Every dashboard budget line equals `GET /budgets/{month}`'s (`allocated` and `spent`) | ✅ Met. Same lines in the same order, plus savings, debt and the totals. |
| Current month's score equals `GET /health-score`'s; a past month shows the last snapshot on or before its end, or `null`; a past month never computes or writes | ✅ Met. Three tests, including the held case and a check that reading past months on a new day writes no snapshot. |
| `as_of` gives the newest transaction and the newest import with rows; both `null` for a household with nothing | ✅ Met. A newer import with no rows is ignored. |
| `auto_budget` or `health_score` disabled → that field is `null`, everything else unchanged | ✅ Met. Compares the whole response with and without each feature. |
| An older client still decodes the response: existing `/dashboard` tests pass unmodified | ✅ Met. `tests/test_dashboard.py` is untouched and passes, 41 tests. |
| No N+1: the statement count doesn't grow with categories or budget lines | ✅ Met. A statement counter compares a household with 2 budgeted categories against one with 8, on the first, budget-generating read and on the steady-state read. |
| `ruff` and `pytest` green; the `database` job's count grew | ⏳ Locally: database tests 470 → 482, lint clean. Confirm in CI. |

Each rule was also checked by breaking it; a test fails each time. The rules broken were: counting rows `countable` excludes, computing a past month, taking `previous_score` from the wrong month, ungating the budget, scoring a future month, counting imports without rows, a query per budget line, and dropping the notice.

## Deviations / decisions

- **The budget shows the user's own lines while learning.** The ticket says `{status, learning}` only. 4.1's review decided lines set by hand show while learning, so Home matches the budget screen.
- **`over` is an amount:** how far spending is past the allocation, `"0.00"` when within it.
- **`savings` and `debt` are included** beside the spending lines, so `total_allocated` and `total_spent` add up to lines that are shown.
- **The score carries `notice`** whenever 4.2 is holding a score, and `scored_on` is the held date then. This keeps it identical to `GET /health-score`.
- **A month after the current one** has `budget` and `health_score` both `null`, and no budget is created by browsing ahead.
- **"An import that saved rows"** means one with at least one transaction, dated by its `created_at`.
- **Reading Home writes.** On the current month it keeps today's score snapshot and settles the month's budget. On any month it settles that month's budget (4.1's rule). This is the same as opening those screens, and why the route now commits.

## Open questions / follow-ups

- **The local suite is slower.** The existing `/dashboard` tests now also build budgets and scores on each read: the database suite took about 2 minutes locally, against about 76 seconds before. No test changed.
- **4.4 (mobile)** reads `budget`, `health_score`, `as_of` and `learning`. Each is optional, so its models should treat them as nullable. It should show `as_of` as "as of …" and the held score's `notice`, worded from its own strings by `code`.
- **Plan gating (7.1)** keeps the same caveat as 4.2: the score is gated by `health_score` but uses budgets regardless of `auto_budget`.
