# Handoff — ticket #65

**Ticket:** [#65](https://github.com/Humble-Coders/Finance-backend/issues/65) — [M5] Goals: create, track and project savings targets

## Summary

`/goals` lets a household set savings goals, gated by a new `goals` feature that is seeded enabled for everyone. For each goal the response shows:
- what's left to save;
- the monthly amount needed to reach the target date;
- the month the planned contribution would finish it;
- progress, and a status: `achieved`, `overdue`, `on_track`, `behind` or `open`.

All of that comes from one pure function, `project`: integer minor units throughout, no growth and no effect of debts. The response labels it `projection_version: "v1"` and `assumes_growth: false`.

**Progress is what the person enters:**
- They can set it directly, or add to it with `POST /goals/{id}/add`. That runs as a single `UPDATE`, so two adds at once both land.
- `achieved_at` is set the first time the target is reached, and cleared when an edit drops the amount back below it.

**Order and limits:**
- `PUT /goals/order` sets the order, and must list exactly the household's goals, each once.
- At most 20 goals can be in progress; the 21st gets `409 goal_limit_reached`, logged through `log_conflict`.

**The learning threshold:** goals work while the household is still learning. Only the comparison against this month's budget waits, and the response says why: `unavailable`, `learning` or `no_savings_line`. The comparison reads the savings line from 4.1's `budget_for`.

**The disclaimer:** `GET /legal/disclaimer` serves the region's not-financial-advice text, which long-term projections carry.

## Files changed

| File | Why |
|---|---|
| `alembic/versions/c2f8e5a71d34_goal_contribution_kind_and_checks.py` | Adds `goal.monthly_contribution_minor_units` (nullable BIGINT) and `goal.kind` (nullable, a new `goal_kind` enum). Adds checks that the target is above 0, and the amount saved and the contribution are at least 0. Undoing it drops the columns, the checks and the enum type. No backfill: the table has never been written to. Follows on from `a9d3e6b14c72`. |
| `alembic/versions/d6a3b8f20e57_seed_goals_feature.py` | The `goals` feature row: global, enabled, with the same `uuid5` scheme as `auto_budget` and `health_score`. |
| `app/models/enums.py`, `app/models/planning.py` | `GoalKind`: the PRD's seven kinds plus `other`, for the app's picture only. The two new `Goal` columns, and the three checks. |
| `app/services/goals.py` (new) | **Projection:** `PROJECTION_VERSION`, `ASSUMES_GROWTH`, `GOAL_LIMIT`, `project()`, `months_through()` and `monthly_need()`. **Saving:** `create`, `edit`, `add_money` (one `UPDATE … RETURNING`, also stamping `achieved_at` with a `CASE`), `reorder` (one `UPDATE` with a `CASE` on id) and `remove`. **Reading and the rest:** `goals_of`, `owned`, `compare_with_budget` and `regional_disclaimer`. |
| `app/schemas/goals.py` (new) | `GoalIn`, `GoalPatch`, `AddMoneyIn`, `GoalOrderIn`, `GoalOut`, `GoalsBudgetOut` and `GoalsOut`. |
| `app/api/goals.py` (new) | The six routes behind `require_feature("goals")`. **Validation errors:** `invalid_amount`, `invalid_name`, `date_in_past`, `invalid_value` (clearing a required field) and `order_mismatch`. **Other answers:** 404 `not_found` for a missing goal or another household's, and 409 `goal_limit_reached`. |
| `app/api/legal.py` | `GET /legal/disclaimer`, the same shape as `/legal/terms`; 404 `no_disclaimer` when the region has none. Not gated by any feature. |
| `app/main.py` | Registers the router. |
| `tests/test_goals_project.py` (new) | 25 tests with no database. |
| `tests/test_goals_api.py` (new) | 37 tests marked `integration`, with "today" pinned to 2026-09-15. Two of them (`TestTwoAddsAtOnce`, `TestTwoCreatesAtOnce`) use two real connections and commit, then clean up after themselves. |
| `tests/test_models.py` | A narrow, documented exemption from the "money is signed" guard for `goal.saved_minor_units` and `goal.monthly_contribution_minor_units`; see Deviations. |

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
   Expect 523 passed (486 on `main`).
4. Run the rest with the same two variables: `pytest -q -m 'not integration'`. Expect 374 passed (349 on `main`).
5. Run `ruff check .` and `ruff format --check .`.

## Acceptance criteria

| Criterion | Status |
|---|---|
| `project` is table-tested with no database: date only, contribution only, both (on track and behind), neither; a target later this month; rounding up ($1,000/3 → $333.34); a currency without cents; a zero contribution; achieved (including saved above target); a past date (overdue) | ✅ Met (`tests/test_goals_project.py`) |
| The same inputs give the same projection, and no float appears in the path | ✅ Met. A guard parses the module and fails on any float literal, any use of `float`, or `/` (true division). |
| Two concurrent `add` calls both land | ✅ Met (`TestTwoAddsAtOnce`): the second waits on the first's row lock, and the total is the sum. |
| `achieved_at` is set when the target is first reached, and cleared when an edit drops below it | ✅ Met |
| `PUT /goals/order` reorders; a missing, foreign or repeated id is `422 order_mismatch` and changes nothing | ✅ Met |
| A household still learning can create and use goals | ✅ Met |
| The budget comparison equals 4.1's savings line; `null` with the right reason when the budget feature is off, while learning, and with no savings line | ✅ Met. It's compared against `GET /budgets/2026-09`'s `savings.allocated`, and each reason is tested. |
| The 21st goal in progress is `409 goal_limit_reached`, through `log_conflict` | ✅ Met on create **and on edit**: an edit that takes an achieved goal back into progress is held to the limit. The count runs under a lock on the household's row, so two creates at once can't both pass (`TestTwoCreatesAtOnce`). An achieved goal doesn't count. The existing guard test confirms `log_conflict` precedes the 409. |
| `GET /goals` carries `projection_version: "v1"` and `assumes_growth: false`, and `disclaimer_version` exactly when a long-term goal exists | ✅ Met |
| `GET /legal/disclaimer` returns the region's regional disclaimer | ✅ Met: `ca-v1` for a CA household, `404 no_disclaimer` with no region. |
| Goals feature off → `403` on every goals route; another household's goal → `404` | ✅ Met |
| Both migrations apply, reverse and reapply in CI | ⏳ Passed locally with `check_migrations.sh`; CI pending |
| ruff and pytest pass; the database job's count grew | ⏳ Locally: database tests 486 → 523, others 349 → 374, lint clean. Confirm in CI. |

Each rule was also checked by breaking it; a test fails each time. The rules broken were:
- the reorder check (a repeated id);
- the learning reason;
- the limit, off by one;
- `achieved_at` never cleared;
- dates in the past allowed;
- the disclaimer attached to any goal, not only long-term ones;
- planned contributions ignored in the comparison;
- an exact target not counting as achieved on add;
- the monthly need rounded down.

## Deviations / decisions

- **The "money is signed" guard has two exemptions (decided by you).** `tests/test_models.py::test_amounts_are_signed` forbids `amount >= 0` checks, because CLAUDE.md treats negative money as legitimate (refunds, debts, overruns). The ticket asks for exactly those checks on a goal's saved amount and contribution. Neither can be negative by nature, so the guard now exempts those two columns, with the reason written beside them. Every other money column is still checked.
- **An overdue goal** has no monthly need, because there are no months left. In the budget comparison it counts its planned contribution if it has one, otherwise nothing.
- **Currency:** a goal takes the household's currency when it's created. The comparison counts only goals in that currency; the list shows all of them.
- **A new goal goes last** in the household's order.
- **Adding to an achieved goal is allowed.**
- **Overflow:** an add that would push the total past what the column holds is `422 invalid_amount`, guarded inside the same `UPDATE`.
- **A past target date can stay put.** `date_in_past` applies only when a date is being set or changed. An overdue goal can be renamed while resending its unchanged date.
- **`PATCH` can clear fields:** sending `null` clears `kind`, `target_date` or `monthly_contribution`. Sending `null` for `name`, `horizon`, `target` or `saved` is `422 invalid_value`.
- **New error code `invalid_name`** for a name that is empty after trimming, or longer than 255 characters once trimmed (review fix: the length was checked before trimming). The ticket set the rule but not the code.
- **The limit holds on every path (review fix).** `_ensure_room` locks the household's row, then counts goals in progress. Both `create` and an `edit` that takes an achieved goal back into progress go through it. The edit's 409 is logged with the reason `open_goal_limit_on_edit`.
- **`PUT /goals/order` answers with the full list,** the same shape as `GET /goals`.

## Open questions / follow-ups

- **Production must be migrated by hand after this merges.** Render never runs Alembic. Production is on `a9d3e6b14c72` and needs `c2f8e5a71d34` and `d6a3b8f20e57`. Until then the goals routes answer 403, because the feature row doesn't exist yet.
- **The ticket's open questions still stand:**
  - investment growth (it would be projection v2);
  - how debts affect long-term goals;
  - the regional disclaimer's text, which is still the seeded draft. Goals is the first screen to show it, so real wording is needed before launch.
- **For 5.2 (mobile): "today" is UTC,** as the ticket and `parse_month` specify. For people in Canada, "this month" moves to the next month a few hours early, on the last evening of each month.
- **For 5.2 (mobile):** projection fields arrive ready to display. `budget_reason` says why the comparison is missing, and `disclaimer_version` says when to show `/legal/disclaimer`.
