# Handoff — ticket #76

**Ticket:** [#76](https://github.com/Humble-Coders/Finance-backend/issues/76) — [M5] Your plan: where the money left over goes, debt payoff first

The ticket is delivered in three PRs, reviewed in order (agreed at `/start-ticket`). This file
grows with each one.

| Stage | What | Status |
|---|---|---|
| 1 | Backend: debts and their balances | Merged (PR #77) |
| 2 | Backend: the plan (split, payoff schedule, preferences, counting fixes, score v4) | **This PR** |
| 3 | App: Your plan, Debts, Home card, Budget link, Goals' suggested amount, balance confirmation on the check screen | To do |

---

## Stage 1: debts and their balances

### Summary
- **Richer debts:** a debt now has a **type**, a **promotional rate and end date**, and a
  **balance with its date and source** (setup, manual or statement).
- **New `/debts` endpoints:** list, add, edit (including "Update balance"), link to the
  household's own card or loan account, and delete.
- **The setup wizard updates its debts in place, by position,** instead of deleting and
  recreating them. A type, promotion, account link or updated balance set on the Debts screen
  survives a wizard save.
- **Saving an import accepts a closing balance and date the person confirmed.** A debt linked to
  that account takes the balance only if the statement closed later than the balance the debt
  already has.

### Files changed
**Data**
- `alembic/versions/b3e7d1f9a2c4_debt_details_and_statement_balance.py`: the new database types
  `debt_kind` and `balance_source`;
  - `debt.kind`, `promo_rate_bps`, `promo_ends_on`, `balance_as_of`, `balance_source`, and
    checks that rates are 0–100% and that a promotional rate and its end come together;
  - `statement_import.closing_balance_minor_units`, `currency` and `closing_date`, with the
    currency length check;
  - existing debts become `other` from `setup`, dated their creation day;
  - reversible.
- `app/models/enums.py`: `DebtKind` and `BalanceSource`.
- `app/models/planning.py`: the new debt columns and checks.
- `app/models/money.py`: the closing-balance columns, plus `currency_check` on
  `statement_import`. The money-column test requires a `currency` column beside every amount.

**Debts**
- `app/services/debts.py` (new):
  - `owned` and `owned_account`: the household's own card or loan accounts only;
  - `is_stale`: more than 45 days old and not from a statement;
  - `take_statement_balance`: newest-only, and the currency must match.
- `app/api/debts.py` (new) and `app/schemas/debts.py` (new): `GET`/`POST /debts` and
  `PATCH`/`DELETE /debts/{id}`. Another household's debt or account is 404, as is an account
  that isn't a card or loan (`unusable_account`).
- `app/main.py`: the router is registered.
- `app/services/financial_setup.py`:
  - `_save_debts` updates the wizard's debts in place by position. A balance sent unchanged
    keeps its date and source, and a changed one is the wizard's, as of today.
  - `rate_bps` and `rate_percent` are now public, so `/debts` uses the same percentage rule.

**Statement save**
- `app/schemas/statements.py`: `ConfirmRowsIn.closing_balance` and `closing_date`, and
  `SaveOutcomeOut.debts_updated`.
- `app/api/statements.py`: `_closing_balance_or_422` (both fields or neither, not negative,
  checked before anything is written), then the import stores the balance and
  `take_statement_balance` runs.

**Tests**
- `tests/test_debts.py` (new, `integration`, 22 tests):
  - **wizard debts:** their defaults; fields the wizard doesn't ask about survive a wizard save;
    a balance changed in the wizard; removed rows go while debts added elsewhere stay;
  - **editing:** a debt with a promotion; each refused value named by field; "Update balance"
    dating and the stale flag; what can and can't be cleared; delete;
  - **linking:** only the household's own card or loan; another household's debt is 404;
  - **statement balance:** updates the linked debt; an older statement never overwrites; no
    balance changes nothing; half or negative values are refused; only the linked debt changes.

**Docs**
- `docs/tickets/M5.4-debt-payoff-plan.md`, and issue #76 updated with the balance-reading
  decision.

### How to test
1. Start a local database (`CLAUDE.md` → Testing). Export **both** `DATABASE_URL` and
   `MIGRATION_DATABASE_URL` pointing at `localhost:55432`, print `migration_dsn` to confirm, then
   run `alembic upgrade head`.
2. Run `pytest -q -m integration` (591 passed) and `pytest -q -m "not integration"`
   (398 passed). Run `ruff check .` and `ruff format --check .`.
3. On a fresh container, run `scripts/check_migrations.sh`. It passed locally.

### Acceptance criteria (the ticket's, as far as Stage 1 reaches)

| Criterion | Status |
|---|---|
| Debts: type, rate, promotion, balance and account link are editable | **Met:** `TestEditing`, `TestLinking` |
| A balance older than 45 days, not from a statement, shows a reminder | **Met:** `balance_stale`, tested |
| An edit survives a later setup-wizard save | **Met:** `test_a_wizard_save_keeps_what_it_does_not_ask` |
| Another household's debt or account is 404 | **Met** |
| Balances from statements: updated with source "statement"; an older statement never overwrites; a missing balance changes nothing and never fails the import | **Met on the server.** The phone reading the balance and the check-screen confirmation come in Stage 3. |
| Migration applies, reverses and re-applies; `ruff` and `pytest` pass; `database` count grew | **Met locally:** 569 → 591. CI's `database` job should show 591. |
| Everything else (the split, schedule, preferences, counting fixes, screens) | Stages 2 and 3 |

### Deviations / decisions
1. **The phone reads the closing balance, not the server.** The balance sits in the summary
   block that the phone's redactor removes before sending, so the server's reader would almost
   never see it. Agreed with the manager on 2026-10-17:
   - the phone reads it before redacting, as it does for the statement period;
   - the person confirms it on the check screen;
   - the save request carries it.

   **No reader or prompt change.** The ticket and the issue are updated.
2. **The wizard and `/debts` share rows by position.** The wizard's payload has no debt ids, so
   row N is the wizard's Nth debt. Debts added from the Debts screen aren't the wizard's
   (`from_setup: false`), and a wizard save never touches them.
3. **A balance the wizard re-sends unchanged keeps its date and source,** so a balance read from a
   statement isn't relabelled "typed at setup" by the wizard echoing what it showed.
4. **Linking to a chequing or savings account is refused** (`unusable_account`). Its closing
   balance is money the person has, not money they owe.
5. **A negative closing balance (a card in credit) is refused,** not applied. There's nothing to
   pay off.
6. **New `statement_import.currency`.** The project's money rule requires a currency column beside
   every amount column. It's filled only when a closing balance is stored.

### Open questions / follow-ups
- **Production migration:** `b3e7d1f9a2c4` must be applied by hand straight after this merges.
  The deployed code reads the new debt columns.
- **Stage 2** reads `kind`, the promotional rate, `balance_source` and `account_id`.
  **Stage 3** sends `closing_balance` and `closing_date`.

---

## Stage 2: the plan

### Summary
- **The plan's arithmetic** lives in `app/services/plan.py`, as pure functions with no
  database.
  - **The split:** the money left over goes first to the buffer (up to its goal's target), then
    the goal share to goals (each up to its monthly need, by priority), then extra to debt while
    any debt is above the rate line, then "Save or invest". The parts add up exactly to the
    money left over.
  - **The schedule:** month by month, with interest rounded half up and a promotional rate until
    its end. Avalanche (highest rate first) or snowball (smallest balance first), and a paid-off
    debt's payment rolls to the next. It also runs a minimums-only comparison and the other
    strategy.
- **New `/plan` endpoints,** gated by the new `debt_plan` feature:
  - `GET /plan?month=` returns the split, each debt's place in the order, its payoff month and
    interest, and the totals;
  - `GET`/`PUT /plan/preferences` read and keep the person's choices (buffer, goal share, rate
    line, strategy). Until they save, the plan runs on suggestions from their own figures.
- **The buffer is an emergency-fund goal.** Choosing a buffer uses the goal the plan already has,
  else the household's own emergency-fund goal, else a new one; its target is the buffer. No
  buffer stops using the goal and keeps it.
- **Counting with links** (the debts linked to accounts in Stage 1):
  - **Score v4:** a payment counts toward the debt it reached, so a card paid in full no longer
    covers a missed loan payment;
  - **the budget's Debt line** leaves out a linked card's minimum while that card's purchases are
    already budgeted.

  With nothing linked, both behave exactly as before.

### Files changed
**Data**
- `alembic/versions/f2a9c47e1b05_plan_preferences.py` (new, revises `b3e7d1f9a2c4`):
  - the `plan_strategy` type and the `plan_preference` table: one row per household; the
    buffer's goal, `ON DELETE SET NULL`; percentage checks; RLS on;
  - seeds the `debt_plan` feature as available everywhere;
  - reversible.
- `app/models/enums.py`: `PlanStrategy`.
- `app/models/planning.py`: `PlanPreference`.

**The plan**
- `app/services/plan.py` (new, pure):
  - `split`;
  - `schedule`, with a 600-month cap, and 12 months without progress meaning never paid off;
  - `rate_on` (the promotional rate until it ends);
  - `suggest_buffer`: one month of average spending, rounded up to a whole unit, else what
    setup was told, else none.
- `app/services/plan_view.py` (new): the I/O half.
  - `build` reads the budget's Savings line, the choices or suggestions, the goals and the debts,
    then calls the arithmetic.
  - `save_preferences` keeps the choices and the buffer's goal.
  - `suggested` averages the last 3 complete months.
- `app/api/plan.py` (new) and `app/schemas/plan.py` (new): the endpoints.
  - **422s:** a bad percentage or month; a buffer of zero or with too many decimals.
  - **409:** a new buffer goal past the 20-goal limit (`goal_limit_reached`).
- `app/main.py`: the `plan` router is registered.

**Counting with links**
- `app/services/debts.py`: `covered_minimums`.
  - A debt linked to an account with transactions in the month is measured on its own: the
    transfers and debt payments that arrived there, up to its minimum.
  - Every other debt shares the rest of the month's debt paid, up to their minimums together.
- `app/services/health_score.py`: formula **v4**. `DebtPicture.covered` and the inputs carry it.
  A v3 snapshot, which has no `covered`, scores exactly as before.
- `app/api/health_score.py`: `covered` is shown in the score's inputs when present.
- `app/services/budget.py`: `_minimums`. A card debt linked to an account with transactions in
  the budget's history window doesn't add its minimum to the Debt line.

**Tests**
- `tests/test_plan_math.py` (new, unit, 18):
  - one debt, month by month ($1,000 at 12%: 11 months, $58.98 of interest);
  - the ticket's Visa, line of credit and car loan under avalanche, with hand-checked payoff
    months and interest;
  - snowball's order and cost;
  - the rate line, and a promotion ending;
  - a payment that never covers the interest; a missing rate; a zero balance;
  - the split's order, a 0% or 100% goal share, a full buffer and no buffer;
  - the suggested buffer.
- `tests/test_plan_api.py` (new, `integration`, 16):
  - preferences: suggestions until saved; the buffer as a goal; an existing emergency-fund goal
    reused; each refused value named by field; the 409;
  - the plan: the money left over equals the budget's Savings line and splits exactly; a
    shortfall splits nothing; a missing rate; still learning; a bad month;
  - `covered_minimums`, linked and unlinked;
  - the budget's `_minimums`.
- `tests/test_health_score*.py`: updated to v4 and `covered`. Two new tests: a v3 snapshot
  doesn't change, and v4 does when a link changes the measure.

### How to test
1. Start a local database (`CLAUDE.md` → Testing). Export **both** `DATABASE_URL` and
   `MIGRATION_DATABASE_URL` pointing at `localhost:55432`, print `migration_dsn` to confirm, then
   run `alembic upgrade head` (head: `f2a9c47e1b05`).
2. Run `pytest -q -m integration` (608 passed) and `pytest -q -m "not integration"`
   (420 passed). Run `ruff check .` and `ruff format --check .`.
3. On a fresh container, run `scripts/check_migrations.sh`. It passed locally.
4. By hand, with a ready household:
   1. give its debt a rate with `PATCH /debts/{id}`;
   2. `GET /plan` and check that `leftover` equals `GET /budgets/{month}`'s Savings line;
   3. `PUT /plan/preferences` and check the plan changes;
   4. `GET /goals` shows the "Emergency fund" goal.

### Acceptance criteria (the ticket's, as far as Stage 2 reaches)

| Criterion | Status |
|---|---|
| Avalanche: the Visa first, the payment rolls on, hand-checked figures | **Met:** `TestAvalanche` |
| Snowball: smallest first; the comparison shows the extra interest | **Met:** `TestSnowball`; `totals.other_strategy_difference` |
| Rate line: a debt below it gets only its minimum | **Met:** `TestTheRateLine`, `test_with_no_debt_above_the_line_goals_then_save_or_invest` |
| Promotions: the promotional rate until the date, the order changes after | **Met:** `test_a_promotion_ending_moves_the_debt_into_the_order` |
| Edge cases: never covers the interest; no rate; nothing guessed | **Met on the server:** `never_paid_off`, `missing_rate`. Putting a debt with no rate last with "Add the interest rate" is Stage 3 (`order` is null for it). |
| The split: buffer, goal share by priority, debt, Save or invest; adds up exactly; a shortfall splits nothing | **Met:** `TestTheSplit`, `test_the_money_left_over_is_the_budgets_savings_and_splits_exactly`, `test_a_shortfall_leaves_nothing_to_split` |
| Choices: suggested from the household's figures; changeable and kept; no buffer, 0% and 100% work | **Met:** `TestPreferences`, `TestTheSuggestedBuffer`, `test_a_goal_share_of_none_or_all` |
| Counting with links: debt paid per debt; the Debt line leaves out a budgeted linked card; unlinked as today | **Met:** `TestDebtPaymentsByDebt`, `TestTheDebtLine`, and the score tests' v3-equals-v4 case |
| One source of truth: the money left over equals the budget's Savings line | **Met:** the same test reads both |
| Guidance only: no product named | **Met on the server:** the split says only "Save or invest". The disclaimer link is Stage 3. |
| Migration applies, reverses and re-applies; `ruff` and `pytest` pass; `database` count grew | **Met locally:** 591 → 608. CI's `database` job should show 608. |
| App checks | Stage 3 |

### Deviations / decisions
1. **"A card's statement is imported" means the card has transactions in the period.** Imports
   aren't tied to months, but a card's transactions are. For the score that's the scored month;
   for the Debt line it's the budget's history window.
2. **The buffer reuses the household's own emergency-fund goal** (the oldest, if several) before
   creating one, so a person who already made "Rainy day" doesn't get a second fund.
3. **Removing the buffer keeps the goal.** It may hold money the person has saved.
4. **Score v4 only changes anything once a debt is linked.** Snapshots from v3 keep their score.
   The formula version changes, so the next score is v4.
5. **While still learning, the plan has no money left over to split,** and the schedule shows
   the debts at their minimums. That still gives each debt a payoff month.
6. **A schedule that runs 12 months without the balance falling is "never paid off",** and the
   totals are then null rather than a guess.

### Open questions / follow-ups
- **Production migration:** `f2a9c47e1b05` must be applied by hand straight after this merges.
  The deployed code reads `plan_preference`, and the `debt_plan` feature row gates `/plan`.
- **Stage 3** builds the app:
  - the Your plan and Debts screens, the Home card, the Budget link and Goals' suggested amount,
    on `/plan` and `/debts`;
  - the phone reads the closing balance, the check screen confirms it, and the save sends
    `closing_balance` and `closing_date`.
