# Handoff — ticket #84

**Ticket:** #84 — [M6] Suggested questions answered without AI

## Summary

The AI coach's six most common questions are now answered with **no model call**.
- **Where the answers come from:** #83's data tools, plus the Budget tab's own figure for the budget question, placed into fixed English sentences.
- **Endpoints:** `GET /chat/suggestions` lists only the suggestions that have something to say. `POST /chat/suggestions/{id}/answer` returns `{text, figures}`, where `figures` holds every amount in the text exactly as the tool returned it.
- **Gate:** both endpoints check `ai_chat` only. There's no AI-consent check and no no-training-tier check, because nothing reaches a model.
- **Cache:** answers are cached in process by household, suggestion, month and `ledger_version`, so any ledger write makes the next answer fresh.
- **Budget figure:** the budget response gains `left_to_spend`, so the Budget tab's "left to spend" is worked out once on the server instead of on the phone.

No migration is needed.

## Files changed

**The suggestions**
- `app/services/chat_templates.py`:
  - **The questions:** `SUGGESTIONS`, the six ids and their English questions.
  - **The answers:** one function per suggestion, each calling a #83 tool or `budget_for`.
  - **The entry points:**
    - `available()` drops any suggestion with nothing to say;
    - `answer()` reads the in-process cache, keyed by `ledger_version`;
    - `BUDGET_FEATURE`.
- `app/api/chat_suggestions.py`: `GET /chat/suggestions` and `POST /chat/suggestions/{id}/answer`, gated by `require_feature("ai_chat")`.
  - An unknown id gets a 404.
  - A suggestion with no data gets a 409 `nothing_to_say`, logged through `log_conflict`.
- `app/schemas/chat.py`: `SuggestionOut`, `SuggestionsOut` and `SuggestionAnswerOut`.
- `app/main.py`: registers the router.

**Budget**
- `app/services/budget.py`: a `BudgetView.left_to_spend` property. It's every line except savings, minus `spent_overall`, and `None` once the month has ended.
- `app/schemas/budget.py` and `app/api/budgets.py`: `left_to_spend` on `BudgetOut`.

**Money**
- `app/core/money.py`: `display(amount, currency)` formats amounts for sentences, e.g. "$1,234.50", "-$12.00", "1,200 JPY". It only formats and never rounds; it raises on more precision than the currency allows.

**Tests**
- `tests/test_chat_suggestions.py`: 22 tests; `tests/test_money.py`: 4 tests for `money.display`, in the fast suite, all `integration`.

## How to test

These steps use the local Docker database only, never `.env`.

1. Point both URLs at the local container:
   ```bash
   export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres
   export MIGRATION_DATABASE_URL=$DATABASE_URL
   ```
2. Run the new tests. Expect 22 passed:
   ```bash
   .venv/bin/pytest -q tests/test_chat_suggestions.py
   ```
3. Run both suites. Expect 525 without the database (521 before) and 733 with it (711 before):
   ```bash
   .venv/bin/pytest -q -m "not integration"
   .venv/bin/pytest -q -m integration
   ```
4. Lint:
   ```bash
   .venv/bin/ruff check app tests && .venv/bin/ruff format --check app tests
   ```
5. To see the answers by hand:
   - run the app locally against the container;
   - set `feature_availability.is_enabled = true` for `ai_chat`;
   - call `GET /chat/suggestions` and `POST /chat/suggestions/spent_last_month/answer`.

For the test household (today pinned to 2026-09-15), the answers are:

| Question | Answer |
|---|---|
| How much did I spend last month? | "You spent $840.00 in August." |
| What's my biggest spending category this month? | "Your biggest category this month is Debt payment: $300.00 of the $507.35 you've spent so far." |
| How does this month compare with last month? | "So far this month you've spent $507.35, against $840.00 in all of August — $332.65 less." |
| How much have I spent this year? | "You've spent $2,867.35 so far in 2026." |
| How much is left in my budget this month? | "You have $242.65 left to spend in your budget this month." |
| Where did most of my money go this year? | "So far this year, most of your money went to LOBLAWS ($1,470.00), VISA PAYMENT ($1,200.00) and BISTRO ($135.00)." |

## Acceptance criteria

| Criterion | Status | Where |
|---|---|---|
| Each answer's figures equal the matching tool or Budget-tab figure, formatted by `money.py`, and are listed in `figures` | Met | `TestEachAnswerIsTheToolsFigure`: six tests, each comparing against the tool called directly, or `GET /budgets` `left_to_spend` |
| No model call: a test fails if `build_client` is called on any suggestion path | Met | The autouse `no_model` fixture patches `build_client` in `llm`, `filing`, `statements`, `plan` and `ai_budget` to fail. `test_nothing_here_reads_consent_or_the_tier` also scans the two modules' source |
| Works without AI consent and without the no-training tier; not counted in the daily chat cap | Met | `test_every_path_works_without_ai_consent_or_a_model`: the household never consents to AI, and all six are answered. Neither module reads consent or the tier. The daily cap doesn't exist yet (6.4), and nothing here counts against it |
| A suggestion with no data is absent from `GET /chat/suggestions` | Met | `test_one_with_no_data_is_left_out`, `test_a_household_with_nothing_is_offered_nothing` |
| A ledger write changes the next answer | Met | `test_a_ledger_write_changes_the_next_answer`: `PATCH` changes August from 840.00 to 850.00. `test_the_same_version_is_served_from_the_cache` proves the cache is used |
| `ai_chat` off returns 403; another household's data never appears | Met | `test_chat_off_is_403` (`ai_chat` is seeded off), `test_another_households_data_never_appears` |
| `ruff`, `pytest` green; `database` count grew | Met locally | 711 → 733; CI still to run on the PR |

## Deviations and decisions

- **The cache is in process, not a table.** The key includes `ledger_version`, so no server can serve a stale answer after a write; a restart only costs recomputing. This means no migration and no production step.
- **`budget_left` isn't cached, and is offered only when `auto_budget` is on.**
  - A budget changes without the ledger changing (a line set by hand), so a version-keyed cache could go stale; the answer is one `budget_for` call, as opening the Budget tab is.
  - Gating it on the Budget tab's own feature (`auto_budget`) stops a household with chat on but budgets off from seeing budget figures through chat.
  - Like `GET /budgets`, reading it may prepare the month's budget, and the endpoint commits that.
- **`left_to_spend` is new on the budget response.** The ticket asks for "the Budget tab's own figure", but the server didn't send it; the app computed it in `BudgetBoard.leftToSpend`. It's now computed once on the server by the same rule, and a test checks the rule.
- **Signs are put into words, not computed.**
  - **`vs_last_month`:** the tool's signed `diff` becomes "$X less" or "$X more", and the figure listed is the tool's diff without its sign.
  - **`budget_left`:** when the budget is overspent, the over amount is the tool's figure without its sign ("You're $X over your budget").
- **Comparing with last month:** `vs_last_month` sets this month so far against all of last month, and says so in the sentence. It's left out when either month has no spending.
- **`money.display`** was added for readable sentences. The API keeps returning plain decimal strings everywhere else, and `figures` holds the plain strings.
- **409 for a suggestion with no data** (for example, a row deleted after the list was fetched), logged with `log_conflict` as the guard test requires. An unknown id gets a 404.

### Changes from review (#87)

- **`Answer.figures` is a tuple.** Answers are cached and shared, so a caller appending to a list would have changed the answer every later caller is given. The API converts it to a list.
- A regression test (`test_a_cached_answer_cannot_be_changed_by_its_caller`) covers it.
- **The `money.display` tests moved to `tests/test_money.py`,** so the fast `test` job covers this pure function too. Before, it was tested only inside the database-marked file.

## Open questions and follow-ups

- **Before `ai_chat` is enabled in production,** run #83's backfill (`python -m app.scripts.rebuild_rollups`). Without it, existing households' older months are missing from the totals these answers read.

- **Product Owner:** the six suggestions and their wording (the ticket's open question). Category names appear as stored, e.g. "Debt payment".
- **6.4:**
  - call `chat_templates.answer(...)` when a message carries `suggestion_id`, store the reply with `answered_by = "template"`, and don't count it against the cap;
  - its grounding check can trust `figures`.
- **6.7 / mobile:** the app can switch from computing `leftToSpend` itself to reading `left_to_spend` from the budget response. That's a separate, small mobile change.
- **`ai_chat` is still `coming_soon` in production,** so these endpoints return 403 there until it's enabled. That's a data change, made when chat launches.
