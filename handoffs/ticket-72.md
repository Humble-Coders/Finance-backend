# Handoff — ticket #72

**Ticket:** #72 — [M6] Hold back advice in chat until there is enough history

## Summary

While a household is still learning, the AI coach answers facts but holds back advice. "Still learning" uses `learning_state`, the threshold the budget, the score and the dashboard already use: 1 complete month and 20 countable transactions.

Before any model call, a check on the server labels the question `fact`, `advice` or `unclear`:
- **advice** gets a fixed learning reply that says what's held back and shows the progress ("0 of 1 full month · 5 of 20 transactions"). No model is called, no consent is needed, and nothing is counted against the daily cap.
- **unclear** goes to the model with a facts-only instruction added after the cached prefix of the prompt.
- **fact** is answered as normal.

A reply that gives advice anyway, from the model or from the cache, is replaced by the learning reply. While learning, every reply carries the progress as `learning` (the budget's `LearningOut` shape). Once the household is ready, `learning` is null and nothing else changes.

## Files changed

**Services**
- `app/services/chat_learning.py` (new):
  - `classify`, where advice wins over fact ("what should I budget?" is advice);
  - `prescriptive`, which spots a reply that instructs;
  - `learning_payload`, in the `LearningOut` shape;
  - `learning_reply`, the progress sentence;
  - `LEARNING_INSTRUCTION`, the facts-only instruction.

  The module docstring states the proposed allowed and held-back split.
- `app/services/chat.py`:
  - reads `learning_state` once per message;
  - adds the advice-while-learning branch after the guardrails and before the cache;
  - re-checks a cache hit with `prescriptive()`;
  - appends `LEARNING_INSTRUCTION` to the system prompt when learning;
  - replaces a prescriptive model reply while learning, and doesn't cache it;
  - adds `Reply.learning`, set on every reply.

**API**
- `app/schemas/chat.py`: `ReplyOut.learning` is typed as `LearningOut` (from `app/schemas/budget.py`).
- `app/api/chat.py`: passes `reply.learning` through.

**Tests**
- `tests/chat_support.py`: `a_learning_household`, with a few transactions, all in the running month.
- `tests/test_chat_learning.py`: 29 tests.
  - 22 need no database: the classification table and the prescriptive patterns.
  - 7 are `integration`.

## How to test

These steps use the local Docker database only, never `.env`.

1. Run the new tests. Expect 29 passed:
   ```bash
   cd ~/AndroidStudioProjects/Finance-backend && DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres MIGRATION_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres .venv/bin/pytest -q tests/test_chat_learning.py
   ```
2. Run every chat test with the same variables. Expect 115 passed:
   ```bash
   .venv/bin/pytest -q tests/test_chat_learning.py tests/test_chat_api.py tests/test_chat_cost.py tests/test_chat_grounding.py
   ```
3. Run both suites. Expect 606 without the database (584 before) and 841 with it (834 before).
4. Lint:
   ```bash
   .venv/bin/ruff check app tests && .venv/bin/ruff format --check app tests
   ```

No migration.

## Acceptance criteria

| Criterion | Status | Where |
|---|---|---|
| A learning household asking a factual question gets a normal grounded answer | Met | `test_a_fact_is_answered_and_the_progress_comes_with_it`: answered by the model, with `learning` filled in |
| A learning household asking a clearly advice question gets the learning reply with correct progress, without the model being called | Met | `test_advice_gets_the_learning_reply_and_no_model`. It uses the `no_model` fixture, works without consent, and leaves no usage row |
| A model that ignores its instructions and replies prescriptively while learning is caught and replaced | Met | `test_a_model_that_advises_anyway_is_replaced` |
| Suggested questions are answered while learning | Met | `test_a_suggestion_is_answered` |
| A ready household gets advice answers as normal (6.4's guardrails still apply) | Met | `test_advice_goes_to_the_model_and_learning_is_null`. The guardrails run before the learning branch, and the 86 earlier chat tests pass unchanged |
| The threshold comes from `learning_state` (a test changes its inputs, not a copy) | Met | `test_the_line_is_learning_states_own`: adding a full August and 20 transactions moves the same question from the learning reply to the model |
| ruff and pytest green; `database` count grew | Met locally | 834 → 841. CI still to run on the PR |

## Deviations and decisions

- **`learning` is on every reply while learning,** facts included, and null once ready. The ticket allowed either; this lets the app show progress beside any answer.
- **A replaced or held-back reply isn't cached.** A cache hit is still re-checked with `prescriptive()` while learning. Readiness can only change with the ledger (`ledger_version` is in the key) or at a month boundary (the day is in the key), so this second check is a backstop.
- **The learning instruction goes at the end of the system prompt,** after the rules and the household's facts, so the cacheable prefix stays the same for learning and ready households.
- **Advice wins over fact** when a question has both ("what should I budget for groceries?").
- **The learning reply is `answered_by: "template"`** when held back before the model. When it replaces a model reply it stays `"model"`, because a model call happened and was counted.

## Open questions and follow-ups

- **For the Product Owner, at `/review-ticket`:**
  - is this the right allowed and held-back split? Allowed: facts, totals, lists, comparisons, explanations. Held back: budgeting, saving, affording, plans, improving the score;
  - should "explain my score" count as a fact? It does here.
- **Production:** #89's migration `e5a1c8d4f702` hasn't been run yet; this ticket adds none. `ai_chat` stays `coming_soon`.
- **6.7 (mobile #54):** render `learning` with the existing `LearningProgress` card.
