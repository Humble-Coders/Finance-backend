# Handoff — ticket #38

**Ticket:** [#38](https://github.com/Humble-Coders/Finance-backend/issues/38) — [M3] Record a transaction the user typed in

## Summary

`POST /transactions` saves one transaction a person typed in. It writes the same row an import writes, with the same `normalized()` key, the same dedup index and the same filing rules. Only two things differ: `source = manual`, and there is no `statement_import_id`.

An exact copy of a recorded transaction (same account, day, amount and description) is refused with **409** `duplicate_transaction`. The response names the match under `detail.duplicate_of`, which is what mobile #30 reads, and nothing is written. Sending `allow_duplicate: true` saves it as the next `occurrence` instead.

Categorizing follows import's order:
- a category the person chose is kept;
- otherwise the household's own rules apply;
- then the AI, but only if the person has consented to AI processing and, in production, the no-training tier is confirmed;
- anything left goes to review.

Editing reuses #37's `PATCH`. There is no migration: `source = manual`, `occurrence` and the dedup index already exist.

## Files changed

| File | Why |
|---|---|
| `app/services/filing.py` (new) | Import's `_apply_categories`, moved unchanged apart from a `may_ask_model` switch. With the switch off, the rules still apply, and whatever they don't cover goes to review rather than to the AI. Import passes `True`, because `/statements/parse` already required consent. |
| `app/api/statements.py` | Calls `file_rows(..., may_ask_model=True)` and loses the moved code. No behaviour change. |
| `app/services/ledger.py` | `save_manual`: converts the amount with `to_minor_units` (in the account's currency), computes `normalized()`, and refuses an exact copy by raising `ExactDuplicate`. `allow_duplicate` takes the highest occurrence + 1, and the insert uses `ON CONFLICT DO NOTHING` then re-reads if it loses a race (3 attempts). The near-match rule is `_near_match` from import. |
| `app/api/transactions.py` | `POST /transactions` (201, `TransactionOut`). `_owned_account` gives the same 404 `unknown_account` (with `field: account_id`) for a missing account and another household's. `_may_ask_model` checks consent and the no-training tier. The 409 calls `log_conflict` first. `TransactionOut` now also carries `source`. |
| `app/schemas/transactions.py` | `ManualTransactionIn` (`extra="forbid"`). It reuses `amount_not_negative` and `date_within_living_memory`. The description is trimmed, then must be 1–512 characters (counted after trimming) and contain a name (it can't normalize to an empty key). `TransactionOut.source`. |
| `tests/test_manual_entry.py` (new) | 24 test functions, 35 cases with the parametrized ones. All are marked `integration`, so CI's database job runs them. |
| `app/api/transactions.py` `_resolve` (review fix) | **A row with no category no longer leaves the review queue.** Confirming it, or PATCHing it without a category, drops the duplicate pointer and switches its reason to `unknown_category`; only a category (PATCH) or deleting it lets it go. The bulk confirm counts only rows that actually left. |
| `app/schemas/statements.py` | The old-date refusal says "is too far in the past"; it no longer says "statement line", because typed entries use it too. |
| `tests/test_transactions_review.py` | Seven #37 tests about leaving the queue now give their rows a category, since an uncategorized row can't leave any more. New `TestAnUncategorizedRowStaysUntilFiled` (4 tests) covers the rule. |
| `tests/test_ledger_endpoint.py` | `use_model` and two "model unavailable" tests now patch `app.services.filing.build_client`, the function's new home. |

## How to test

```bash
docker start finai-pg
```

```bash
ruff check . && ruff format --check .
```

```bash
pytest -q
```

That last one is CI's `test` job, with no database: **251 passed**, 333 skipped.

```bash
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres pytest -q -m integration
```

That is CI's `database` job: **333 passed**. `main` has 294; the 39 extra are this ticket's 35 cases plus the 4 review-queue tests.

**Don't run everything in one go with `DATABASE_URL` set.** That mixes the fast and database tests in one process, and locally 74 of them fail with asyncpg "attached to a different loop". The same 74 fail on untouched `main` (checked by stashing this branch). CI never runs them that way.

**Break-it checks.** Each change was reverted afterwards, and each was caught by the test named:
- Ignore `allow_duplicate` → `test_keeping_both_takes_the_next_occurrence_each_time`.
- Always let the AI be asked → `test_without_consent_the_model_is_never_asked`, `test_production_without_the_no_training_tier_never_asks_the_model`, and the review-queue test.
- Drop the near-match flag → `test_same_day_and_amount_with_another_name_is_flagged_not_refused`. This test first survived the break; it now picks a category, so the flag is the only thing that can put the row in review.
- Accept an empty key → the two "no name" 422 cases.
- Let an uncategorized row leave the queue again → the 4 `TestAnUncategorizedRowStaysUntilFiled` tests and `test_an_uncategorized_near_match_still_asks_for_a_category`.
- Remove the 409's `log_conflict` → `test_an_exact_copy_is_refused_naming_the_match`.

**By hand, once deployed.** Use mobile #30 (PR Humble-Coders/FinAI-Mobile-2026#37), handoff step 9:
1. Save an entry. It succeeds.
2. Save the same entry again. The dialog names the first one.
3. Tap **Yes, keep both**. It saves.

## Acceptance criteria

| Criterion | Status |
|---|---|
| Saved with `source = manual`, no `statement_import_id`, and the same `normalized_description` import computes | **Met.** `test_it_is_the_row_an_import_would_write_marked_manual` |
| Amounts round-trip exactly as decimal strings; no `float` | **Met.** `to_minor_units` / `from_minor_units` only. The tests cover `"1200.5"`→`"1200.50"`, `"0.01"`, and JPY (`"1200"` kept; `"1200.50"` refused rather than rounded). |
| 409 naming the match in full and writing nothing; `allow_duplicate` → next occurrence, then the one after; a test asserts the exact shape under `detail` | **Met.** `test_an_exact_copy_is_refused_naming_the_match` checks `detail.code`, all four `duplicate_of` fields, the row count, and the conflict log line. Occurrences go `[1, 2, 3]`. An imported row is matched too. |
| No category → categorized with household corrections applied; a given category kept exactly | **Met.** A chosen category is kept and the AI is not called. A household rule files the row without the AI. With consent, the AI gets only `[merchant, amount]`. |
| Future date, negative amount, missing account and another household's account refused with the right status and field | **Met.** 422 naming `occurred_on` / `amount` / `description` / `direction` / unknown fields. 404 `unknown_account`, `field: account_id`, byte-identical for missing and not-yours. One day ahead is allowed (timezones). |
| `ruff check`, `ruff format --check`, `pytest` pass; CI green | **Met locally.** CI is pending the push. |

## Deviations / decisions

The manager made four decisions on 2026-09-29:
1. **No AI consent (or, in production, no no-training tier) → rules, then review.** The AI is never asked for someone who hasn't agreed to it. This mirrors the two gates `/statements/parse` applies. A person typing their first few entries may have done neither.
2. **A description with no name is refused (422).** "12345" normalizes to an empty key. Such a row can't be categorized, can't be recognized next month, and would count as a duplicate of every other such entry.
3. **Near matches are flagged, as import does.** Same account, same day, same amount, different description: the row is saved, marked `needs_review`, and points at the other row. `NEAR_MATCH_DAYS` is still 0.
4. **No `PUT`.** #37's `PATCH` edits any owned row. `TestEditing` proves it works on a manual row: it corrects, refuses a would-be duplicate, and has no import to finish.

Also:
- **404, not 422, for an unknown account.** This matches import's `POST /statements/{id}/transactions`, which also takes `account_id` in the body, so a client handles one answer for it.
- **The currency comes from the account**, not the household default import uses. A typed entry names its account, and the account records what it holds.
- **Grouping separators are refused** (`"1,234.56"` → 422 on `amount`). The money boundary doesn't guess; the phone already sends the normalized form.
- **`TransactionOut` gains `source`.** It's additive; the review screen can ignore it.

**From the manager review (PR #48):**
- **A row with no category never leaves the review queue without one.** This applies to every row, not only manual ones. A row carries a single reason, so a manual entry that was both uncategorized (no AI consent) and a near match showed only the duplicate question. Confirming it then released the row with no category, and M4's budgets would silently miss it. Import had the same gap when the AI was unreachable. Now confirming (a retry included) or PATCHing without a category leaves the row waiting, as `unknown_category`. **This changes #37's confirm:** an `unknown_category` row can no longer be "accepted as is"; the person picks a category (`other` exists for what fits nothing) or deletes the row.
- The 512-character description limit is counted after trimming. The raw field is capped at 2048, only to bound the work.
- The old-date message no longer says "statement line".

## Open questions / follow-ups
- **The phone's review screen (when built) must expect confirm to leave a row waiting**, now asking for a category (`needs_review: true`, `review_reason: unknown_category` in the response), and offer the category picker next rather than treating confirm as the end.

- **A flagged manual entry looks the same as any other on the phone.** Mobile #30 shows "Transaction saved" and doesn't yet show `needs_review` / `review_reason`, so a near-match or an uncategorized entry reaches the review queue without the person being told. Worth a line on the phone's confirmation, as a small mobile follow-up.
- **The review queue will show every manual entry from someone without AI consent** (as `unknown_category`), unless a rule covers it. That is decision 1 working as intended, but it's worth watching once people use it.
- **Deploy order:** this must be live before mobile #30's Save works. Nothing else depends on it.
