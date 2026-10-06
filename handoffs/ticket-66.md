# Handoff — ticket #66

**Ticket:** [#66](https://github.com/Humble-Coders/Finance-backend/issues/66) — [M5] Count goal progress in the Money Health Score (formula v2)

## Summary

The Money Health Score gains its fourth part, `goal_completion`, as formula **v2**.

**Which goals count:** goals with a target date, created before the last complete month.

**How a goal is scored:**
- **In progress:** by pace, on a straight line from its creation month to its target month, both counted. The score is `saved` against `target × elapsed ÷ total`, capped at 100. Past its date, it's judged against the whole target.
- **Achieved:** 100 for 12 months, then it drops out.
- **The part:** the plain average over the counting goals.

**The weights** are 34 / 29.75 / 21.25 / 15: v1's 40 / 35 / 25 scaled to 85%. So a household with no counting goals keeps exactly its v1 score.

**Old snapshots still read:** a breakdown with no goals reads as none, which is what v1 counted, so each saved snapshot still reproduces its own score.

**The change label on Home:** `/dashboard`'s `previous_score` is now null when last month's snapshot was scored with a different formula, so the day v2 ships shows no "change" that is only the formula moving.

## Files changed

| File | Why |
|---|---|
| `app/services/health_score.py` | **Formula:** `FORMULA_VERSION = "v2"`, `GOAL_COMPLETION`, Decimal `WEIGHTS` (34 / 29.75 / 21.25 / 15), `ACHIEVED_COUNTS_MONTHS = 12`. **Calculation:** a `GoalPace` input and a pure `goal_completion(goals, last_complete_month)`, summed in sorted order so the result is the same in any order. **Inputs:** `ScoreInputs` gains `goals` and `last_complete_month`, both defaulted. `to_json` writes them; `inputs_from_json` treats them as missing on a v1 breakdown. `current_score` reads the household's goals through `_goal_paces`, with months in UTC. The module docstring describes v2. |
| `app/api/dashboard.py` | `_previous_score` takes the formula version of the score shown, and returns null when there's no score or when last month's snapshot has a different version. |
| `tests/test_health_score_goals.py` (new) | 24 tests with no database, covering `goal_completion`, the v2 score, the exact match with v1 when no goal counts, reordering, and v1 and v2 breakdowns. |
| `tests/test_health_score_api.py` | 5 new database tests, plus updates: the version is now `v2`, and the component list includes `goal_completion` (unavailable with no goals). |
| `tests/test_health_score.py` | The version assertions are now `v2`. All the arithmetic tests pass unchanged. |

No migration.

## How to test

1. Start a fresh local database and bring it to the latest migration:
   ```
   docker run -d --name finai-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 \
     ghcr.io/pgmq/pg17-pgmq:v1.5.1@sha256:e6f893a793751ed30c89f5f88e95aa52b77c1a03440b7d118a996866489ac0c6
   MIGRATION_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres alembic upgrade head
   ```
2. Run the database tests:
   ```
   DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres \
     MIGRATION_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres \
     pytest -q -m integration
   ```
   Expect 528 passed (523 on `main`).
3. Run the rest with the same two variables: `pytest -q -m 'not integration'`. Expect 398 passed (374 on `main`).
4. Run `ruff check .` and `ruff format --check .`.

## Acceptance criteria

| Criterion | Status |
|---|---|
| `goal_completion` table-tested with no database: exactly on pace → 100; half → 50; ahead → capped; created this month or last → not counted; no date → not counted; achieved → 100 for 12 months, then out; past its date → judged against the whole target; several → plain average; none → unavailable | ✅ Met (`TestPace`, `TestWhichGoalsCount`) |
| **Renormalisation:** with no counting goals, the score matches v1 exactly | ✅ Met (`test_with_no_goal_counting_it_is_exactly_v1`, compared with a v1 reference on five varied inputs). This needed the weights changed; see Deviations. |
| Today's snapshot saved as `v2`; yesterday's v1 snapshot unchanged, and still reproduces its score through `result_from_json` | ✅ Met (`test_yesterday_s_v1_snapshot_is_left_as_it_was`): the row is byte-for-byte as stored, and it also reproduces through `score(inputs_from_json(...))`. |
| No change label across versions: v1 last month → `previous_score` null; v2 → the earlier score | ✅ Met (`TestNoChangeAcrossFormulas`) |
| Same inputs, same score, in any order; no float, no AI import | ✅ Met: the reordering test, plus the existing guards that parse the module's code. |
| ruff and pytest green; the database job's count grew | ⏳ Locally 523 → 528 database tests, 374 → 398 others. Confirm in CI. |

**Also tested end to end:**
- Goals reach the score through `/health-score`: the `goal_completion` part shows a score of 50 with weight `15.00`, and the overall score is 93.
- A goal created last month isn't counted yet.

Each rule was also checked by breaking it, and a test failed every time. The rules broken were:
- the version check behind the change label;
- a goal made last month counting;
- a 13-month window for achieved goals;
- elapsed months off by one;
- the ticket's round weights;
- a v1 breakdown that fails to read;
- undated goals counting;
- goals never read.

## Deviations / decisions

- **Weights 34 / 29.75 / 21.25 / 15, not the ticket's 35 / 30 / 20 / 15 (decided by you).** The ticket's weights broke its own acceptance criterion: once goals are left out, 35:30:20 doesn't reduce to v1's 40:35:25, so a household without goals would have shifted by up to about a point on release. Scaling v1's three weights to 85% keeps goals at exactly 15% and those households exactly where v1 had them. The weights are now exact `Decimal`s, not `int`s.
- **Achieved goals follow the same eligibility rule** (a target date, created before the last complete month). Otherwise a goal made and filled the same day would score 100 for a year.
- **The 12-month window is measured against the last complete month,** so `goal_completion(goals, last_complete_month)` needs nothing else, as the ticket's signature has it.
- **`saved` is today's amount,** compared with where the goal should have been by the end of last month. Goals keep no history (5.1), so there's nothing better to use.
- **Months are counted with both ends included:** "total" runs from the creation month through the target month, and "elapsed" from the creation month through the last complete month.
- **`previous_score` is also null whenever no score is shown,** not only when the versions differ.

## Open questions / follow-ups

- **For the Product Owner:** the ticket's four questions still stand: the weights (now 34 / 29.75 / 21.25 / 15), straight-line pace, the 12-month window for achieved goals, and the plain average. Each is a constant or a single rule in `health_score.py`; changing any of them means bumping the formula version.
- **Ordering with mobile (5.2):** 5.2 adds the `goal_completion` sentence. Until it ships, Home's breakdown (4.4) lists the new part under "Another part of the score" with no sentence.
- **No migration;** nothing needs running on production after merging.
