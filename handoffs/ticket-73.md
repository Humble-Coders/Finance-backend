# Handoff — ticket #73

**Ticket:** [#73](https://github.com/Humble-Coders/Finance-backend/issues/73) — [M4] Stop counting money moved between your own accounts as income or spending

## Summary

**One counting rule.** Rows filed as Transfers or Savings are no longer counted as income or
expenses. That applies everywhere a flow is summed:
- net and the previous month;
- the trend and the daily running balance;
- "Where your money went".

Savings still shows on the investments card, and Debt payment is still spending. The rule is
`is_flow()`, next to `countable()` in `app/services/dashboard.py`.

**Pairing** (`app/services/transfers.py`) runs after categorisation on every import save and
manual entry, against rows already saved, so the import order doesn't matter.
- **What pairs:** a debit and a credit on two different accounts of the household, with the
  same amount and currency, within 5 days, where there is evidence of a transfer (see Deviations).
- **What changes:** the side that isn't already filed correctly becomes Transfers and goes to
  Review as `own_transfer`. Both rows get `transfer_pair_id`.
- **Card payments:** a payment received on a card account, filed as income or "other", is
  always a transfer. A bank-side payment with no partner keeps counting.

**Debt paid counts the card being paid, once.** When the bank side becomes a transfer, the
card's payment received (a transfer credit on a card account) counts as debt paid instead. So the
debts card and the score's debt-payments part keep counting the payment, whichever statements are
imported. This was added after review.

**The Budget's Debt line deliberately differs from Home's "debt paid."** The Budget asks "where did
the money go?". When the card statement is imported, its purchases already answer that in their
own lines, so the payoff is not more money spent. The Debt line holds only debt payments not visible
as purchases: loans, and a bank-side card payment whose card statement isn't imported.
- A second-review change that made the two agree counted the card bill twice in the Budget's total
  and in its learned plan. It was reverted after the third review.
- Tests now pin both sides: the purchases or the payment, never both.

**Health score** moves to formula `v3`: the same arithmetic over the corrected income and
expenses, so "vs last month" is hidden across the change.

**Free re-parse.** At the import limit, a statement that was read but never saved, read again
within 24 hours, is replaced instead of counting again (at most 3 a day). Saving the replaced
import returns `409 import_superseded`.

## Files changed

**Counting**
- `app/services/dashboard.py`: the shared flow rule:
  - `TRANSFERS_SLUG`, `NOT_A_FLOW` and `is_flow()`;
  - used in `_sums_by_direction`, `figures_by_month` (income and expenses only), `_daily` and
    `spend_by_category`;
  - each needs the outer join on `Category`.
- `app/services/dashboard.py`: `paid_card_debt()`, added after review.
  - `figures_by_month`'s `debt_paid` is now Debt payment debits **plus** card payments received.
  - The debts card's `moved` reads that figure instead of `_moved_by_category`.
  - The score reads the same `debt_paid`.
- `app/services/budget.py`: **unchanged from `main`**. A second-review change here was reverted after
  the third review; see the Summary.

**Pairing**
- `app/services/transfers.py` (new): `pair_transfers`, with the module docstring giving the
  rules and why they lean towards not pairing.
- `app/api/statements.py`: `confirm_rows` calls `pair_transfers` after `file_rows`, and reports
  `earlier_needs_review`.
- `app/schemas/statements.py`: `SaveOutcomeOut.earlier_needs_review` (additive, defaults to 0), added
  after review. It counts rows from earlier imports that this save sent back to Review;
  `needs_review` still counts only this import's rows.
- `app/api/transactions.py`: `add_transaction` calls it, with `chosen=` set when the person
  picked the category.
- `app/models/enums.py`: `ReviewReason.own_transfer`.
- `app/models/money.py`: `Transaction.transfer_pair_id`, a self-reference with ON DELETE SET
  NULL.
- `alembic/versions/e4b7a2c9d13f_review_reason_own_transfer.py`: adds the enum value, the
  column and the foreign key. The downgrade clears `own_transfer` reasons, then rebuilds the
  enum type.

**Score**
- `app/services/health_score.py`: `FORMULA_VERSION = "v3"`, and the docstring explains why.

**Re-parse**
- `app/api/statements.py`: `_replace_unsaved` (called only at the limit); `confirm_rows` refuses
  a superseded import with 409 through `log_conflict`.
- `app/config.py`: `import_rereads_per_day = 3`.

**Tests**
- `tests/test_transfers.py` (new, `integration`, 30 tests):
  - a card bill in both import orders, card only, and bank only then the card;
  - a refund;
  - savings with one side imported and with both;
  - ties, two rows wanting one partner, the nearest partner winning, outside 5 days;
  - a person's rule, a doubtful amount, groceries never re-filed, another household;
  - manual entry, with and without a chosen category;
  - every total agreeing;
  - a fixed query count;
  - added after review:
    - debt paid with the bank only, the card only, and both;
    - a loan payment staying Debt payment, counted once;
    - a suspected duplicate never being a partner;
    - an investment purchase never hiding pay;
    - `earlier_needs_review` reporting the bank row a card import re-filed;
  - added after the third review (`TestTheBudgetCountsACardBillOnce`): a card bill raises the
    Budget's total spent by its purchases or by the bank's payment, never both:

    | Imported | Debt line | Total spent rises by |
    |---|---|---|
    | Bank only | $1,300 | $1,000 |
    | Card only | $300 | $600 |
    | Both | $300 | $600 |

    Home's debt paid reads $1,300 in all three cases. The learned Debt history ignores the card's
    payment received.
- `tests/test_statements_endpoint.py`: `TestReadingAgain` (3 tests). The three quota tests now
  save the first import before the second read, because an unsaved re-read is free by design.
  They use `save_parsed`, with no model.
- `tests/test_dashboard.py`, `tests/test_dashboard_additions.py`, `tests/test_budgets_api.py`:
  updated to the new rule. Savings and transfers are out of expenses and the breakdown, and the
  budget's spend still reconciles with the dashboard's.
- `tests/test_health_score*.py`: the current version is `v3`. A new test checks a v2 score last
  month gives no change; the "gives the change" test now uses v3.

**Docs**
- `docs/tickets/M4.6-own-account-transfers.md`: the ticket, with the corrections agreed at
  `/start-ticket` (issue #73 updated to match).

## How to test

1. Start a throwaway database, as in `CLAUDE.md` → Testing (container `finai-pg`, port 55432).
2. Migrate it. **Export both URLs.** `MIGRATION_DATABASE_URL` in `.env` points at production, and
   `DATABASE_URL` alone does not override it:
   ```
   export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres
   export MIGRATION_DATABASE_URL=$DATABASE_URL
   .venv/bin/python -c "from app.config import get_settings; print(get_settings().migration_dsn)"  # must say localhost:55432
   .venv/bin/alembic upgrade head
   ```
3. Run the suites (separately, as in earlier tickets):
   ```
   .venv/bin/pytest -q -m integration      # 562 passed
   .venv/bin/pytest -q -m "not integration" # 398 passed
   .venv/bin/ruff check . && .venv/bin/ruff format --check .
   ```
4. Rehearse the migration on a **fresh** container:
   ```
   docker run -d --name finai-pg-mig -e POSTGRES_PASSWORD=postgres -p 55433:5432 \
     ghcr.io/pgmq/pg17-pgmq:v1.5.1@sha256:e6f893a793751ed30c89f5f88e95aa52b77c1a03440b7d118a996866489ac0c6
   DATABASE_URL=postgresql://postgres:postgres@localhost:55433/postgres \
     PG_CONTAINER=finai-pg-mig PYTHON=.venv/bin/python scripts/check_migrations.sh
   docker rm -f finai-pg-mig
   ```
   This passed locally ("All migration checks passed").

## Acceptance criteria

| Criterion | Status | Where |
|---|---|---|
| Card bill, both statements, either order: expenses $1,000, income unchanged, breakdown once, both rows `transfers` + `own_transfer` | **Met** | `TestACardBill::test_bank_first_then_card`, `test_card_first_then_bank` |
| Card statement only: payment is `transfers` + `own_transfer`, not income | **Met** | `test_the_card_alone_files_its_payment_as_a_transfer` |
| Bank statement only: payment still counts; the card statement later pairs it | **Met** | `test_the_bank_alone_still_counts_the_payment` |
| Savings: no change to expenses, net not lowered, savings rate rises, investments card shows it | **Met for expenses, net and the investments card** (`TestSaving`). The savings rate is **reasoned, not tested**: it is `net / income` over the same `figures_by_month` the tests check, but no test scores it. |
| No false pairs: ties, same import, person-categorised, suspected duplicates | **Met** | `TestNoFalsePairs`. Same import is excluded because a pair needs two accounts and an import is one account. Low confidence: `test_a_doubtful_amount_is_never_paired`. Suspected duplicates: `test_a_suspected_duplicate_is_never_a_partner`. Investment purchases: `test_an_investment_purchase_never_hides_pay`. |
| Every total uses the same rule | **Met** | `TestEveryTotalAgrees` (month, trend, daily, breakdown) and `test_spent_is_the_dashboard_s_count` (budget) |
| Score: new snapshots are `v3`; no change across v2 → v3; v1 and v2 snapshots still load | **Met** | `test_health_score_api.py` (`TestNoChangeAcrossFormulas`, the v1 snapshot test) |
| Pairing adds one query per import, whatever the row count | **Met, reworded** | At most **4** reads per save, for 1 row or 30 (`TestCost`). See Deviations. |
| Re-parse: free re-read within 24 hours, superseded save is 409, the 4th replacement or a read after a saved import is 429 | **Met** | `TestReadingAgain`, `TestQuota::test_the_second_import_in_a_month_is_refused` |
| Migration applies, reverses and re-applies in CI | **Met** | CI's `database` job and `check_migrations.sh` (empty database). The downgrade was also rehearsed by hand on a fresh container **with a paired `own_transfer` pair present**: reasons cleared to NULL, rows still `needs_review`, then the re-upgrade was clean. |
| `ruff` and `pytest` pass; `database` job count grew | **Met** | Integration tests went from 528 on `main` to 562 (+34: 30 transfers, 3 re-read, 1 score). CI's `database` job should show 562. |

## Deviations / decisions

These were agreed at `/start-ticket` and recorded in the ticket under "Corrections agreed at
`/start-ticket`":

1. **"Never saved" means no transactions point at the import**, not `confirmed_at` empty.
   `confirmed_at` is only set once Review is finished, so the ticket's test would have replaced
   saved imports.
2. **Which rows count as the person's own category:**
   - a merchant with a household correction rule;
   - a hand-typed row whose category the person picked (`chosen=`);
   - an *earlier* hand-typed row with any category, since nothing records who picked it.
3. **A card payment is told apart from a refund by the categoriser's answer** (income, transfers
   or other), not by keyword lists. Those would be per-language code, and the categoriser only
   ever sees merchant and amount.
4. **Pairing runs after `file_rows`.**

Decided while building:

5. **New column `transfer_pair_id`.** The ticket said pairing changes only category and reason.
   "One partner each" needs a record of the pair: without it, a bank payment already paired
   could be claimed by a second card's payment of the same amount.
6. **A pair needs evidence.** Equal opposite amounts alone (pay going in, rent going out on the
   card) are not enough. The evidence is one of:
   - the credit lands on a card, loan, savings or investment account;
   - the debit leaves a **savings** account. Investment accounts were removed after review:
     their statements list purchases as debits, and one matching a pay cheque would hide income;
   - either side is already filed as Transfers or Savings.

   A side filed as spending (Groceries, etc.) is never re-filed.
7. **Some sides keep their category, and only re-filed rows go to Review.**
   - Savings stays Savings, so the investments card still counts it as set aside.
   - A bank-side Debt payment to a **loan** account stays Debt payment, because a loan's
     statement has no purchases and the payment really is debt paid down.
   - A paired side that already fits is linked but not flagged; the ticket said "both to Review".
8. **Query count:** a fixed ceiling (at most 4 reads) instead of "one query". The new rows,
   their candidates, the household's rules and the Transfers category each need a read; none
   depends on the row count.
9. **Debt paid includes card payments received** (after review). Once the bank side is a
   transfer, the card's side is the record of the card being paid; exactly one side is ever
   counted. A loan is excluded, because its bank side stays Debt payment. The rule applies on
   Home and in the score only. The Budget's Debt line doesn't use it, because the card's purchases
   are already budgeted (see the Summary).
10. **Re-parse is checked only at the limit.** With a larger allowance, two different statements
   read one after the other are both wanted, so nothing is replaced. Only the newest unsaved
   import is replaced.

## Open questions / follow-ups

- **Production migration.** `e4b7a2c9d13f` must be applied by hand after merge (Render never
  migrates). Until it is, the new code selects `transfer_pair_id` and fails. Check prod's
  `alembic_version` read-only and ask the manager before running `alembic upgrade`.
- **Past rows are not re-filed.** The new counting rule applies to past months straight away,
  because totals are computed when read. Pairing only reaches past rows when a new save lands
  within 5 days of them. A one-off backfill is out of scope.
- **An import whose rows were all exact duplicates** (saved 0) has no transactions, so it counts
  as "unsaved" and can be replaced by a re-read; saving it again then returns 409. This is
  harmless (it saved nothing), but 4.7 should treat `import_superseded` as "read again", as its
  ticket says.
- **The mobile app** shows `own_transfer` as an unknown reason until 4.7 ([FinAI-Mobile-2026#55](https://github.com/Humble-Coders/FinAI-Mobile-2026/issues/55)) adds the wording.
- **Known, conservative edge: a cancelled match can still double count.** When a tie or the
  two-claimant rule cancels a match, the card rule still files the card's payment received as a
  transfer, but the bank side stays Debt payment. In that rare case the payment still counts as
  spending on top of the purchases, as it did before this PR, and debt paid counts it twice (both
  sides). The person sees the card row in Review. Not pairing is the safer mistake.
- **Known race, accepted as for the limit:** a save of import A running at the same moment as a
  re-read that replaces A can save both statements on one allowance. This is the same
  check-then-act trade-off as the monthly limit (see the comment in `parse`).
- **Pre-existing, not changed here: the Debt line's allocation includes every listed debt's
  minimum, a Visa's included, while the Visa's purchases are also budgeted in categories.** With the
  card statement imported, the line can look under-paid against that minimum. This is the same root
  cause as the next point, and the same follow-up fixes both.
- **Pre-existing, not changed here: any card payoff counts toward "debt paid".** A household that
  pays its card in full but misses a **loan** payment can still score 100 on debt payments
  (paid ≥ required), exactly as the bank-side Debt payment did before this PR. Counting only
  payments towards the debts the person listed needs debts linked to accounts (`Debt.account_id`
  is optional, and the setup wizard doesn't set it), which is a product decision. **Suggested
  follow-up ticket: link debts to accounts.** Then debt paid counts only payments towards listed
  debts, and a card whose statement is imported leaves the Budget's Debt line, because its
  purchases are its budget.
- **Recategorising one side of a pair** leaves the other side and the link alone, as the ticket
  says. If people often undo pairs, clearing the link on recategorise is a small follow-up.
