# Handoff — ticket #71

**Ticket:** #71 — [M6] The AI coach: chat endpoints, guardrails and grounded numbers

## Summary

People can now hold a conversation with the AI coach at `/chat/conversations`: list, create, read, post a message and delete. Each message takes the cheapest answer available:
1. a suggestion chip, answered by #84's template;
2. a server-side guardrail, for securities picks or prompt injection;
3. the answer cache, keyed by #83's `ledger_version`;
4. only then the model.

**Gates before any model call:** AI-processing consent (409, logged), the no-training tier in production (503), and a daily cap (429). None of the first three answers needs these gates.

**The model** runs #69's tool loop with:
- #83's data tools;
- the app's own screens (the endpoints' own answers, offered only when the household has that screen's feature);
- #70's `run_sql`, last;
- the last six turns as text only, a 400-token answer and the per-question token budget.

**Every figure in a reply is checked** against that turn's tool results, the question, and earlier grounded answers. Anything else gets one retry naming the strays, then an honest fallback.

**A migration** adds the answer cache and daily-usage tables and seeds ai-v3 of the AI-processing policy as an undated draft.

## Files changed

**Schema and settings**
- `alembic/versions/e5a1c8d4f702_chat_cache_usage_ai_v3_draft.py`:
  - `chat_answer_cache`, unique on household, question hash and `ledger_version`, with `expires_at`;
  - `chat_daily_usage`, unique on household and day;
  - both cascade on household delete and have row level security on;
  - ai-v3 seeded undated, with a paragraph on what chat sends.

  The downgrade reverses all of it.
- `app/models/derived.py` and `app/models/__init__.py`: `ChatAnswerCache` and `ChatDailyUsage`.
- `app/config.py` and `.env.example`:
  - `llm_chat_model`, empty meaning `LLM_MODEL`;
  - `chat_daily_message_limit`, default 30.

**Services**
- `app/services/chat.py`: the orchestrator.
  - **The order:** suggestion, then guardrails, then cache, then the gates, then the model.
  - **The model run:** grounding with one retry, then the truncation note, the reply guardrail, the disclaimer flag, the stored turns and one `chat_turn` log line.
  - **Also here:** `forget_untouched` removes conversations untouched for 365 days.
- `app/services/chat_grounding.py`:
  - `figures_in` reads amounts, percentages and counts, with or without `$`, thousands separators, decimals and negatives. Dates, years and counts of 10 or less are excepted.
  - `grounded_values` and `ungrounded` do the checking.
- `app/services/chat_guardrails.py`:
  - the securities-pick, injection and reply checks;
  - `forward_looking` for the disclaimer;
  - `stands_alone`, for the cache;
  - `normalized_hash`;
  - the cached `system_prompt`, which includes #70's `SCHEMA_PROMPT`, the data range and today.
- `app/services/chat_service_tools.py`: the `budget`, `dashboard`, `goals`, `debts`, `plan` and `health_score` tools.
  - Each calls its endpoint's own function.
  - Results are compacted: no ids, no version stamps, and no heavy series such as the daily chart and the score history.
  - A tool is offered only when its feature is on.
  - `HTTPException` becomes a `ToolError`.

**API**
- `app/api/chat.py`: the five endpoints, gated by `require_feature("ai_chat")`.
  - Another household's conversation is a 404.
  - A `ChatRefused` becomes its status and code.
- `app/schemas/chat.py`: `ConversationOut`, `ConversationsOut`, `MessageOut` (text only), `ConversationDetailOut`, `MessageIn` (text of at most 1,000 characters, or a `suggestion_id`) and `ReplyOut` (`text`, `disclaimer`, `answered_by`, and `learning`, which stays null until 6.5).
- `app/main.py`: registers the router.

**Tests**
- `tests/chat_support.py`: the scripted model `Scripted`, the `call`, `say` and `say_from` steps, and the household and request helpers. The `model` and `no_model` fixtures are registered in `tests/conftest.py`.
- `tests/test_chat_grounding.py`: 45 cases, no database.
- `tests/test_chat_api.py`: 24 tests, `integration`.
- `tests/test_chat_cost.py`: 6 tests, `integration`.

## How to test

These steps use the local Docker database only, never `.env`.

1. Migrate the local container:
   ```bash
   DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres MIGRATION_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres .venv/bin/python -m alembic upgrade head
   ```
2. Run the new tests with the same two variables. Expect 75 passed:
   ```bash
   .venv/bin/pytest -q tests/test_chat_grounding.py tests/test_chat_api.py tests/test_chat_cost.py
   ```
3. Run both suites. Expect 578 without the database (525 before) and 829 with it (799 before).
4. Run the migration round trip, which needs an empty throwaway container. It passed locally.
5. Lint:
   ```bash
   .venv/bin/ruff check app tests alembic && .venv/bin/ruff format --check app tests alembic
   ```

No LLM key is needed; every test uses the scripted model.

## Acceptance criteria

| Criterion | Status | Where |
|---|---|---|
| A question answered from a tool result; the stored conversation holds the user turn, the compact tool record and the reply; `GET` returns text only | Met | `test_it_is_answered_from_a_tool_and_stored`, `test_reading_it_back_shows_text_only` |
| **Ten years:** `spending_summary(2016-01, 2025-12, by year)` gives a grounded reply; a truncated listing is described as a summary | Met | `TestTenYears`. A truncated result without "summary" in the reply gets `TRUNCATION_NOTE` added by the server |
| No model call for a `suggestion_id` message or a cache hit; a ledger write misses the cache; a follow-up with "that" never uses the cache | Met | `test_chat_cost.py::TestNoModelCall`; the `no_model` fixture fails if a client is built |
| Only the text of the last 6 turns reaches the model; no earlier tool result is re-sent | Met | `test_only_the_text_of_the_last_six_turns` |
| **Grounding:** an absent figure is retried once, then the honest fallback; a grounded reply passes; tables cover currency formats, percentages, separators and negatives | Met | `TestGrounding`, plus `test_chat_grounding.py` |
| **Guardrails:** securities picks refused; prompt injection declined; forward-looking answers set `disclaimer: true` | Met | `TestGuardrails`: refused before the model, and replaced if the model picks one anyway |
| **Gates:** `ai_chat` off → 403; no consent → `409 consent_required` via `log_conflict`; production without the tier → 503; past the cap → 429; templated and cached answers pass without consent and aren't counted | Met | `TestGates`, `test_the_same_question_again_is_a_cache_hit` |
| One household can never read, post to or delete another's conversation (404) | Met | `test_another_households_conversation_is_404` |
| Conversations untouched for 12 months are removed; account deletion removes conversations and cached answers | Met | `test_a_year_untouched_and_it_is_gone`. No account-deletion endpoint exists yet, so `test_deleting_the_household_deletes_conversations_and_cache` deletes the household row and checks all three chat tables cascade |
| No message text, SQL or results in logs; `chat_turn` carries tokens and cost | Met | `test_it_carries_tokens_and_never_the_text` |
| Migration applies, reverses and re-applies; ruff and pytest green; `database` count grew | Met locally | `check_migrations.sh` passed; 799 → 829. CI still to run on the PR |

## Deviations and decisions

- **The daily cap is counted in its own table,** `chat_daily_usage`, not from the stored messages, so deleting a conversation can't give questions back.
  - Every model attempt counts, including a failed one, because it cost tokens.
  - The check is check-then-act, as in statement import: two simultaneous requests at the limit can both pass.
- **Grounding also allows figures from the question and from earlier grounded answers.** Without the second, a follow-up such as "what was the total again?" would repeat an earlier figure without a tool call and always end in the fallback. Earlier answers' figures were themselves grounded when stored.
- **Guardrails are checked on the server before and after the model.**
  - A securities pick or an injection attempt gets a fixed reply (`answered_by: "template"`) and never reaches a model.
  - A reply that tells someone to trade a security is replaced.
  - `disclaimer` is set when the reply, the question ("can I afford…") or the tools used (plan, goals, health score) are about what's to come.
- **"Stands alone" is wider than the ticket's pronoun rule.** A question with "again", "instead", "also", "what/how about", or one that opens with "and", "but" or "so", is a follow-up too. Without that, "what was the total again?" would be cached and served in another conversation.
- **The truncation rule is enforced by the server:** when a tool result is truncated and the reply doesn't say "summary", a fixed sentence is added.
- **The screen tools call the endpoints' own functions,** so the coach can't disagree with the screen. Reading the budget may prepare and commit the month's budget, as opening the Budget tab does.
- **A model failure** (`LlmError`) returns an honest "couldn't answer just now" as a normal reply, stored and counted, rather than an HTTP error.
- **The order of the gates** follows the ticket: consent, then tier, then cap. Statement import checks the tier first.
- **`updated_at` uses `clock_timestamp()`,** not `now()`, so conversations touched in one transaction don't tie when sorting newest first.

## Open questions and follow-ups

- **Production, after merge, by hand, with approval:** run the migration `e5a1c8d4f702`.
- **`ai_chat` stays `coming_soon`.** Before it's turned on for real users:
  - the paid no-training host is in place (`LLM_NO_TRAINING_TIER`);
  - the ai-v3 wording is final and dated, with legal review;
  - `finai-api` is resumed on Render (it's suspended now);
  - one synthetic round trip is tried with the dev key, to check that Gemini's OpenAI-compatible endpoint accepts `content: null` on assistant turns that only call tools (the #69 note).
- **6.5 (#72):** fills `learning` on the reply, and the learning-phase branch goes into `post_message`.
- **The cap's race:** a strict cap would need a lock or a conditional upsert. It isn't done, as in statement import.
- **A lawyer's review** of the guidance-not-advice wording and the disclaimer (ticket open question 3).
