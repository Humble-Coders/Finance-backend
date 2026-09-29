# Handoff — ticket #42

**Ticket:** [#42](https://github.com/Humble-Coders/Finance-backend/issues/42) — [M3] Let a user withdraw consent to AI processing

## Summary

A person can now withdraw consent to AI processing (`DELETE /legal/ai-processing/consent`), and give it again with the existing `POST`. Withdrawal is recorded as an event in a new `consent_change` table. The `consent_event` row that proves what was agreed to is never deleted or edited, so the log reads "given, withdrawn, given again".

`has_consented`, the one gate, respects the latest change. After a withdrawal:
- `/statements/parse` answers its existing `409 consent_required`, which the app already handles;
- a transaction typed in (#38) is filed by the household's own rules or left for the person, never sent to the AI.

A new `GET /legal/ai-processing/consent` tells the phone's Settings row which state to show.

Policy version `ai-v2` restores the withdrawal sentence and covers typed-in entries. It is seeded as a **draft**: ai-v1 stays in force and nobody is asked to consent again yet.

## Files changed

| File | Why |
|---|---|
| `alembic/versions/a238d28cd057_consent_change.py` | A new `consent_action` enum and the `consent_change` table: `user_id` (deleted with the user), `kind`, `action`, the version given (empty for a withdrawal), and timestamps. Indexed on `(user_id, kind, created_at)`, with row-level security on. **Purely additive**, so production works whether this migration or the deploy lands first. |
| `alembic/versions/4f4b1595e986_seed_ai_policy_v2_draft.py` | `ai-v2`, with `effective_from` empty (a draft). It is ai-v1's text plus two paragraphs: what a typed-in entry sends, and what withdrawal does and doesn't do. Deliberately **no** mention of account deletion, which isn't built. |
| `app/models/enums.py`, `app/models/identity.py`, `app/models/__init__.py` | `ConsentAction` and `ConsentChange`. |
| `app/services/ai_consent.py` | `has_consented`: the latest change decides; with no changes, `consent_event` decides as before. `record_consent`: writes the consent row (no-op if it exists) and a `given` change. `withdraw`: idempotent, and records nothing if there's nothing to withdraw. Both lock the user's row, and stamp changes with `clock_timestamp()`. |
| `app/api/legal.py`, `app/schemas/legal.py` | `DELETE` and `GET /legal/ai-processing/consent`, both answering `{consented, version}`. `POST` now goes through `record_consent`. Log lines carry only the user id. |
| `app/config.py` | A note next to `llm_no_training_tier`: **put ai-v2 in force before turning this on.** |
| `tests/test_ai_consent_withdrawal.py` (new) | 14 tests, all marked `integration`. |
| `tests/test_models.py` | `consent_change` joins `consent_event` in the "reached through the user" list, because consent belongs to a person, not a household. |

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

No database, as in CI's `test` job: **253 passed**.

```bash
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres pytest -q -m integration
```

CI's `database` job: **347 passed**, up from 333 on `main`; the 14 extra are this ticket's.

The migration check is `scripts/check_migrations.sh`. It passed ("All migration checks passed") against a throwaway container on port 55433, because it needs an empty database.

**Break-it checks.** Each change was reverted afterwards.

| Change | What caught it |
|---|---|
| The gate ignores a withdrawal | 5 tests: parse refused, legacy withdrawal, the re-consent sequence, the typed entry, status |
| Re-consent writes the wrong change | 6 tests |
| Withdrawing twice records twice | `test_withdrawing_twice_records_one_withdrawal` |
| A withdrawal is recorded with nothing to withdraw | `test_withdrawing_without_ever_consenting_is_a_quiet_no_op` |
| `clock_timestamp()` dropped | 5 tests, on every one of three runs, because the order of changes would come down to a random id |

**By hand, after deploy.** With a session token:
1. `GET /legal/ai-processing/consent` returns `{"consented": true, "version": "ai-v1"}` for someone who consented.
2. `DELETE` the same path. It returns `{"consented": false, ...}`.
3. `POST /statements/parse` returns 409 `consent_required`.
4. `POST /legal/ai-processing/consent` with `{"version": "ai-v1"}`. Parsing works again.

## Acceptance criteria

| Criterion | Status |
|---|---|
| After withdrawal, `/statements/parse` returns 409 `consent_required` and no model call is made | **Met.** `test_after_withdrawal_parsing_is_refused_and_no_model_is_called` (model calls = 0) |
| The consent given is still readable after withdrawal: given-then-withdrawn, not an absence | **Met.** `test_the_consent_given_is_still_there_after_withdrawal`: one `consent_event`, and changes `[given, withdrawn]` |
| Re-consenting after withdrawal succeeds and parsing works again | **Met.** `test_re_consent_works_and_the_log_shows_the_whole_sequence`: parse 200, still one `consent_event`, changes `[given, withdrawn, given]` |
| Withdrawing twice, or without ever consenting, is a no-op | **Met.** Both are 200. One withdrawal row, or none. |
| Transactions imported before the withdrawal are untouched | **Met.** `test_transactions_imported_before_are_untouched` |
| One household's withdrawal never affects another's consent | **Met.** `test_one_person_s_withdrawal_leaves_another_s_consent_alone`: the other still consents and parses |
| Ruff, pytest, CI green | **Met locally.** CI pending |

The ticket's scope also asked for:
- **Re-consent** with a full log. Met.
- **Copy saying what withdrawal does and does not do.** Done in `ai-v2`, with a test on its text.
- **Restoring the withdrawal sentence as a new version.** `ai-v2`, a draft.

## Deviations / decisions

The manager decided these on 2026-09-29:
1. **Storage is a change log beside `consent_event`**, not a `withdrawn_at` column. A `withdrawn_at` column would have meant relaxing `consent_event`'s one-row-per-version rule, and every consent write would then fail between the production migration and the deploy. The log is additive, and `consent_event` stays the untouched proof of the text agreed to.
2. **`ai-v2` is a draft.** It says "in Settings", and the phone has no such row yet. Dating it is a one-line migration when the mobile row ships. **At that moment, everyone who agreed to ai-v1 is asked again**, as a changed policy requires (`test_a_new_policy_in_force_needs_consent_again` shows it).
3. **`ai-v2` covers typed-in entries** (#38: their shop name and amount).
4. **The PRD:** a small PR adding withdrawal to Appendix A.5 §1 in the mobile repo (linked from this PR).

Also:
- **`GET /legal/ai-processing/consent`** goes beyond the ticket's list. The Settings row can't offer "withdraw" or "give consent" without knowing which applies, and nothing reported it.
- **`ai-v2` doesn't mention account deletion**, although the ticket's copy note refers to it. Account deletion isn't built, and a policy pointing at it would be the same false claim ai-v1's draft had to drop. Add it in the version that goes live after it exists.
- **Withdrawal is of the processing, not of a version.** A withdrawal row has no version. It outranks consent to any version, so consent to ai-v1 can't "survive" a withdrawal made while ai-v2 was in force.

## Production database

Both migrations were applied to production by the developer after this PR was opened (see the PR comment for the before and after revision). They are additive, and `ai-v2` goes in undated, so nothing changes for anyone until the code deploys, and nobody is asked to re-consent.

## Open questions / follow-ups

- **Date `ai-v2`** (a one-line migration) when the mobile Settings row for withdrawing ships. **This must happen before `LLM_NO_TRAINING_TIER` is turned on in production**, as the note in `app/config.py` says.
- **The mobile Settings row** (not yet ticketed on the mobile side, as far as I can see). It reads `GET /legal/ai-processing/consent`, withdraws with `DELETE`, and reuses the existing consent screen to consent again.
