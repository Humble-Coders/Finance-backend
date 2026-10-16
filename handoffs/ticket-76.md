# Handoff — ticket #76

**Ticket:** [#76](https://github.com/Humble-Coders/Finance-backend/issues/76) — [M5] Your plan: where the money left over goes, debt payoff first

The ticket is delivered in three PRs, reviewed in order (agreed at `/start-ticket`). This file
grows with each one.

| Stage | What | Status |
|---|---|---|
| 1 | Backend: debts and their balances | **This PR** |
| 2 | Backend: the plan (split, payoff schedule, preferences, counting fixes, score v4) | To do |
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
