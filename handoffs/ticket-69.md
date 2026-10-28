# Handoff — ticket #69

**Ticket:** #69 — [M6] Teach the LLM client to call tools — cheaply

## Summary

`app/services/llm.py` can now hold a tool-calling conversation with a model, on both
transports. It has new types (`Message`, `ToolSpec`, `ToolCall`, `AssistantTurn`, `RunResult`,
`ToolError`) and a `converse(...)` method on the protocol, implemented for the
OpenAI-compatible transport and for Anthropic. The `run_tools(...)` helper drives the loop:
- it runs only registered handlers;
- it stops at 3 rounds, a 45-second deadline, or a per-question token budget;
- it returns malformed arguments to the model once before giving up.

Cost controls are built in:
- **Caching:** the system prompt and tools always lead each request, and Anthropic gets `cache_control`.
- **Small results:** `tool_result(...)` sends minified JSON capped at 4 KB, with a `truncated` flag.
- **Tracking:** `Usage` records cached input tokens and cost. Cost is the provider's own figure when it reports one; otherwise it is an estimate from `LLM_PRICES`, marked as estimated.

`complete(...)` is unchanged and thinking stays off. No route or model call is added; this is plumbing for 6.4.

## Files changed

**Service**
- `app/services/llm.py`:
  - the new types, `converse` on both transports, `run_tools`, `tool_result`, `estimate_cost`;
  - a small JSON-schema checker for tool arguments;
  - cached-token and cost fields on `Usage`;
  - tool names (only) in the `llm_call` log line.

**Settings**
- `app/config.py`:
  - `llm_chat_token_budget` (6,000), the per-question budget 6.4 will pass in;
  - `llm_prices`, the per-model price table (USD per million tokens) used for estimates.
- `.env.example`: documents `LLM_CHAT_TOKEN_BUDGET` and `LLM_PRICES`.

**Tests**
- `tests/test_llm_tools.py` (32 tests): fake HTTP via `httpx.MockTransport`, so no key and no model.
  - They run against both transports and the recorded request bodies.
  - They cover every acceptance criterion below.

## How to test

```bash
cd Finance-backend
export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres
export MIGRATION_DATABASE_URL=$DATABASE_URL
.venv/bin/pytest -q tests/test_llm_tools.py            # 32 passed
.venv/bin/pytest -q tests/test_llm_client.py tests/test_statement_parsing.py tests/test_categorization_batches.py
.venv/bin/pytest -q -m "not integration"                # 487 passed
.venv/bin/pytest -q -m integration                      # 676 passed (needs local Postgres)
.venv/bin/ruff check app tests && .venv/bin/ruff format --check app tests
```

Optional real round trip: use the dev key from the team password manager, with synthetic data only. Call `run_tools` from a REPL with one fake handler, and never commit the key.

## Acceptance criteria

| Criterion | Status | Where |
|---|---|---|
| Both transports: tool requested, run exactly once with parsed args, then final text | Met | `TestTheLoop::test_a_tool_runs_once_and_its_result_shapes_the_answer` (gemini + anthropic) |
| Two calls in one turn both run; results in the provider's shape (`tool` + `tool_call_id`; `tool_result` blocks) | Met | `test_two_calls_in_one_turn_both_run_and_both_go_back` |
| Stops at `max_rounds`, deadline and token budget with `LlmError`; never runs an unregistered tool | Met | `test_it_stops_at_the_round_limit`, `…_at_the_deadline`, `…_when_the_token_budget_is_spent`, `test_an_unregistered_tool_is_never_run` |
| Malformed args back once as a tool error; second raises `LlmError` | Met | `test_malformed_arguments_go_back_once_then_end_the_run`; non-JSON in `TestOpenAiShapes` |
| Caching: prefix byte-identical and first; Anthropic `cache_control`; cached tokens reach `Usage` | Met | `TestPromptCaching`, `test_anthropic_marks_the_stable_prefix_for_caching` |
| Over-cap result truncated with `truncated: true`; results minified | Met | `TestToolResult` |
| `complete(...)` unchanged; existing LLM, statement and categorization tests pass; thinking off | Met | 66 existing tests pass unchanged; `converse` uses the same `_thinking()` fields as `complete` |
| Log capture: no tool argument or result text in any log line | Met | `test_no_tool_argument_or_result_is_ever_logged` |
| `ruff check`, `ruff format --check`, `pytest` pass | Met | Commands above |

## Deviations / decisions

- **Cost:** the provider's reported cost is kept when present (OpenRouter reports one). The `LLM_PRICES` estimate is used only as a fallback and sets `Usage.cost_estimated`. A model with no price logs no cost rather than a guess. This was agreed at planning ("keep real cost with estimate fallback").
- **`token_budget` defaults to `None` (no budget) in `run_tools`.** The client module stays free of chat settings; 6.4 passes `settings.llm_chat_token_budget`.
- **Argument validation** is a small built-in JSON-schema subset, so no new dependency:
  - type, enum, minimum/maximum;
  - required, `additionalProperties: false`;
  - nested properties and items.
- **Errors raised by a handler:** a `ToolError` goes back to the model as a tool error. Anything else the handler raises propagates and is not hidden from the caller.
- `run_tools` refuses (`ValueError`) handlers that have no matching `ToolSpec`, so nothing can be run that the model was not told about.

## Open questions / follow-ups

- **Prices:** fill `LLM_PRICES` in Render for the production model (`google/gemini-2.5-flash` via OpenRouter reports its own cost, so this matters mainly after a provider switch).
- **Gemini implicit caching** only applies above a minimum prefix size. 6.4's system prompt plus tool definitions should be checked against it once written.
- **Stale comment:** the `.env.example` line "Statement parsing is the only thing that calls a model" becomes untrue when 6.4 lands; update it there.
- **Untracked ticket docs:** the M4–M6 ticket docs under `docs/tickets/` are still untracked in this repo and are not part of this branch.
