"""Tool calling in the LLM client (#69): both transports, the loop's limits,
prompt caching, compact results, cost, and what is never logged.

No model and no key: every response is a fake served through
`httpx.MockTransport`, and every request body is captured to check its shape.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from structlog.testing import capture_logs

from app.config import Settings
from app.services.llm import (
    TOOL_RESULT_MAX_BYTES,
    LlmError,
    Message,
    ToolError,
    ToolSpec,
    build_client,
    estimate_cost,
    run_tools,
    tool_result,
)

SECRET = "LOBLAWS-4412"

SPENDING = ToolSpec(
    name="spending_summary",
    description="Spending over a period, grouped.",
    parameters={
        "type": "object",
        "properties": {
            "from_month": {"type": "string"},
            "to_month": {"type": "string"},
            "group_by": {
                "type": "string",
                "enum": ["year", "month", "category", "none"],
            },
        },
        "required": ["from_month", "to_month", "group_by"],
        "additionalProperties": False,
    },
)
GOALS = ToolSpec(
    name="goals",
    description="The household's goals.",
    parameters={"type": "object", "properties": {}},
)

GOOD_ARGS = {"from_month": "2016-01", "to_month": "2025-12", "group_by": "year"}


def settings(**overrides) -> Settings:
    base = dict(
        database_url="postgresql://localhost/x",
        supabase_url="https://example.supabase.co",
        llm_api_key="key",
        llm_provider="gemini",
        llm_model="m",
    )
    return Settings(**{**base, **overrides})


def wired(provider: str, responses: list[dict], **overrides):
    """A real client serving [responses] in turn, and the request bodies it sent."""
    sent: list[dict] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=queue.pop(0))

    client = build_client(settings(llm_provider=provider, **overrides))
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, sent


# --- Provider responses ----------------------------------------------------------


def openai_calls(*calls: tuple[str, str, object], usage: dict | None = None) -> dict:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": args
                                if isinstance(args, str)
                                else json.dumps(args),
                            },
                        }
                        for call_id, name, args in calls
                    ],
                },
            }
        ],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 10},
    }


def openai_text(text: str, usage: dict | None = None) -> dict:
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": text}}],
        "usage": usage or {"prompt_tokens": 120, "completion_tokens": 20},
    }


def anthropic_calls(*calls: tuple[str, str, dict], usage: dict | None = None) -> dict:
    return {
        "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "id": call_id, "name": name, "input": args}
            for call_id, name, args in calls
        ],
        "usage": usage or {"input_tokens": 100, "output_tokens": 10},
    }


def anthropic_text(text: str, usage: dict | None = None) -> dict:
    return {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
        "usage": usage or {"input_tokens": 120, "output_tokens": 20},
    }


def calls_for(provider: str):
    return openai_calls if provider != "anthropic" else anthropic_calls


def text_for(provider: str):
    return openai_text if provider != "anthropic" else anthropic_text


class Recorder:
    """Handlers that record what they were run with."""

    def __init__(self):
        self.runs: list[tuple[str, dict]] = []

    def handlers(self):
        async def spending(args: dict) -> dict:
            self.runs.append(("spending_summary", args))
            return {
                "rows": [{"y": 2016, "t": "4820.15"}],
                "row_count": 1,
                "truncated": False,
            }

        async def goals(args: dict) -> dict:
            self.runs.append(("goals", args))
            return {"rows": [], "row_count": 0}

        return {"spending_summary": spending, "goals": goals}


async def ask(client, recorder: Recorder, **limits):
    return await run_tools(
        client,
        system="You are the coach.",
        messages=[Message.user("How has my spending changed?")],
        tools=[SPENDING, GOALS],
        handlers=recorder.handlers(),
        **limits,
    )


PROVIDERS = ["gemini", "anthropic"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
class TestTheLoop:
    async def test_a_tool_runs_once_and_its_result_shapes_the_answer(self, provider):
        client, sent = wired(
            provider,
            [
                calls_for(provider)(("c1", "spending_summary", GOOD_ARGS)),
                text_for(provider)("Spending was $4,820.15 in 2016."),
            ],
        )
        recorder = Recorder()

        result = await ask(client, recorder)

        assert result.text == "Spending was $4,820.15 in 2016."
        assert result.rounds == 2
        assert recorder.runs == [("spending_summary", GOOD_ARGS)]
        assert result.tool_results[0][0] == "spending_summary"
        assert "4820.15" in result.tool_results[0][1]
        # The result went back in the provider's own shape.
        second = sent[1]["messages"]
        if provider == "anthropic":
            block = second[-1]["content"][0]
            assert (block["type"], block["tool_use_id"]) == ("tool_result", "c1")
        else:
            assert (second[-1]["role"], second[-1]["tool_call_id"]) == ("tool", "c1")

    async def test_two_calls_in_one_turn_both_run_and_both_go_back(self, provider):
        client, sent = wired(
            provider,
            [
                calls_for(provider)(
                    ("c1", "spending_summary", GOOD_ARGS), ("c2", "goals", {})
                ),
                text_for(provider)("Done."),
            ],
        )
        recorder = Recorder()

        await ask(client, recorder)

        assert [name for name, _ in recorder.runs] == ["spending_summary", "goals"]
        messages = sent[1]["messages"]
        if provider == "anthropic":
            # Both results in one user message, as Anthropic requires.
            ids = [b["tool_use_id"] for b in messages[-1]["content"]]
            assert ids == ["c1", "c2"]
        else:
            assert [m.get("tool_call_id") for m in messages[-2:]] == ["c1", "c2"]

    async def test_it_stops_at_the_round_limit(self, provider):
        again = calls_for(provider)(("c1", "goals", {}))
        client, sent = wired(provider, [again, again, again])
        recorder = Recorder()

        with pytest.raises(LlmError, match="within 3 rounds"):
            await ask(client, recorder, max_rounds=3)
        assert len(sent) == 3
        # The third round's tools are not run: no round is left to read them.
        assert len(recorder.runs) == 2

    async def test_it_stops_at_the_deadline(self, provider):
        client, _ = wired(provider, [calls_for(provider)(("c1", "goals", {}))])
        ticks = iter([0.0, 50.0])

        with pytest.raises(LlmError, match="deadline"):
            await ask(client, Recorder(), clock=lambda: next(ticks))

    async def test_it_stops_when_the_token_budget_is_spent(self, provider):
        client, _ = wired(provider, [calls_for(provider)(("c1", "goals", {}))])

        with pytest.raises(LlmError, match="token budget"):
            await ask(client, Recorder(), token_budget=50)

    async def test_an_unregistered_tool_is_never_run(self, provider):
        client, _ = wired(provider, [calls_for(provider)(("c1", "drop_tables", {}))])
        recorder = Recorder()

        with pytest.raises(LlmError, match="unregistered"):
            await ask(client, recorder)
        assert recorder.runs == []

    async def test_malformed_arguments_go_back_once_then_end_the_run(self, provider):
        bad = {"from_month": "2016-01"}  # missing fields
        client, sent = wired(
            provider,
            [
                calls_for(provider)(("c1", "spending_summary", bad)),
                calls_for(provider)(("c2", "spending_summary", bad)),
            ],
        )
        recorder = Recorder()

        with pytest.raises(LlmError, match="malformed"):
            await ask(client, recorder)
        assert recorder.runs == []
        # The first went back to the model as a tool error naming the problem.
        returned = json.dumps(sent[1]["messages"])
        assert "required" in returned

    async def test_a_handlers_tool_error_goes_back_to_the_model(self, provider):
        client, sent = wired(
            provider,
            [
                calls_for(provider)(("c1", "spending_summary", GOOD_ARGS)),
                text_for(provider)("That range has no data."),
            ],
        )

        async def refusing(args: dict) -> dict:
            raise ToolError("to_month is in the future")

        result = await run_tools(
            client,
            system="s",
            messages=[Message.user("q")],
            tools=[SPENDING],
            handlers={"spending_summary": refusing},
        )

        assert result.text == "That range has no data."
        assert "in the future" in json.dumps(sent[1]["messages"])


@pytest.mark.asyncio
class TestOpenAiShapes:
    async def test_arguments_that_are_not_json_count_as_malformed(self):
        client, _ = wired(
            "gemini",
            [
                openai_calls(("c1", "goals", "{not json")),
                openai_calls(("c2", "goals", "{not json")),
            ],
        )

        with pytest.raises(LlmError, match="malformed"):
            await ask(client, Recorder())

    async def test_a_reply_cut_off_at_the_limit_is_an_error(self):
        cut = {
            "choices": [{"finish_reason": "length", "message": {"content": "Spend"}}],
            "usage": {},
        }
        client, _ = wired("gemini", [cut])

        with pytest.raises(LlmError, match="cut off"):
            await ask(client, Recorder())


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
class TestPromptCaching:
    async def test_the_system_prompt_and_tools_are_identical_and_first_every_call(
        self, provider
    ):
        client, sent = wired(
            provider,
            [
                calls_for(provider)(("c1", "goals", {})),
                text_for(provider)("Done."),
            ],
        )

        await ask(client, Recorder())

        first, second = sent
        assert first["tools"] == second["tools"]
        if provider == "anthropic":
            assert first["system"] == second["system"]
        else:
            assert first["messages"][0] == second["messages"][0]
            assert first["messages"][0]["role"] == "system"
        # What varies comes after: the second call only adds to the end.
        assert len(json.dumps(second["messages"])) > len(json.dumps(first["messages"]))

    async def test_cached_tokens_reach_usage(self, provider):
        usage = (
            {"input_tokens": 200, "cache_read_input_tokens": 1_500, "output_tokens": 30}
            if provider == "anthropic"
            else {
                "prompt_tokens": 1_700,
                "completion_tokens": 30,
                "prompt_tokens_details": {"cached_tokens": 1_500},
            }
        )
        client, _ = wired(provider, [text_for(provider)("Hi.", usage=usage)])

        await ask(client, Recorder())

        assert client.usage.input_tokens == 1_700
        assert client.usage.cached_input_tokens == 1_500


@pytest.mark.asyncio
async def test_anthropic_marks_the_stable_prefix_for_caching():
    client, sent = wired("anthropic", [anthropic_text("Hi.")])

    await ask(client, Recorder())

    body = sent[0]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in body["tools"][0]


@pytest.mark.asyncio
class TestCost:
    async def test_the_providers_own_cost_is_kept(self):
        client, _ = wired(
            "openrouter",
            [
                openai_text(
                    "Hi.",
                    usage={"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0021},
                )
            ],
            llm_prices={"m": {"input": 100.0, "output": 100.0}},
        )

        await ask(client, Recorder())

        assert client.usage.cost == pytest.approx(0.0021)
        assert client.usage.cost_estimated is False

    async def test_without_one_the_price_table_estimates_and_says_so(self):
        client, _ = wired(
            "gemini",
            [
                openai_text(
                    "Hi.",
                    usage={
                        "prompt_tokens": 1_000_000,
                        "completion_tokens": 1_000_000,
                        "prompt_tokens_details": {"cached_tokens": 500_000},
                    },
                )
            ],
            llm_prices={"m": {"input": 0.30, "cached_input": 0.075, "output": 2.50}},
        )

        await ask(client, Recorder())

        assert client.usage.cost == pytest.approx(0.15 + 0.0375 + 2.50)
        assert client.usage.cost_estimated is True

    async def test_without_a_price_there_is_no_cost_rather_than_a_guess(self):
        client, _ = wired("gemini", [openai_text("Hi.")])

        await ask(client, Recorder())

        assert client.usage.cost is None


def test_an_estimate_needs_input_and_output_prices():
    assert (
        estimate_cost(
            {"input": 1.0}, input_tokens=1, cached_input_tokens=0, output_tokens=1
        )
        is None
    )
    assert (
        estimate_cost(None, input_tokens=1, cached_input_tokens=0, output_tokens=1)
        is None
    )


class TestToolResult:
    def test_it_is_minified(self):
        assert (
            tool_result({"rows": [{"a": 1}], "row_count": 1})
            == '{"rows":[{"a":1}],"row_count":1}'
        )

    def test_over_the_cap_rows_are_cut_and_it_says_so(self):
        rows = [{"merchant": "Amazon", "amount": "12.99"} for _ in range(500)]

        text = tool_result({"rows": rows, "row_count": 500}, max_bytes=1_000)

        body = json.loads(text)
        assert len(text.encode()) <= 1_000
        assert body["truncated"] is True
        assert body["row_count"] == 500
        assert 0 < len(body["rows"]) < 500

    def test_a_bare_list_counts_as_rows(self):
        body = json.loads(tool_result([{"a": "x" * 50}] * 100, max_bytes=500))

        assert body["truncated"] is True
        assert body["row_count"] == 100


@pytest.mark.asyncio
async def test_no_tool_argument_or_result_is_ever_logged():
    client, _ = wired(
        "gemini",
        [
            openai_calls(("c1", "spending_summary", {**GOOD_ARGS})),
            openai_text("Your spending."),
        ],
    )

    async def leaky(args: dict) -> dict:
        return {"rows": [{"merchant": SECRET, "t": "4820.15"}]}

    with capture_logs() as logs:
        await run_tools(
            client,
            system="s",
            messages=[Message.user(f"what about {SECRET}?")],
            tools=[SPENDING],
            handlers={"spending_summary": leaky},
        )

    written = json.dumps(logs, default=str)
    assert SECRET not in written
    assert "4820.15" not in written
    assert "2016-01" not in written
    calls = [entry for entry in logs if entry["event"] == "llm_call"]
    assert calls[0]["tools"] == ["spending_summary"], "names are logged, not arguments"


def test_handlers_without_a_spec_are_refused():
    async def nothing(args: dict) -> dict:
        return {}

    client, _ = wired("gemini", [])
    with pytest.raises(ValueError, match="without a tool spec"):
        asyncio.run(
            run_tools(
                client,
                system="s",
                messages=[Message.user("q")],
                tools=[SPENDING],
                handlers={"spending_summary": nothing, "secret": nothing},
            )
        )


# --- Regressions from review (#85) -----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
class TestReviewRegressions:
    async def test_a_final_answer_past_the_budget_is_still_returned(self, provider):
        heavy = (
            {"input_tokens": 5_000, "output_tokens": 2_000}
            if provider == "anthropic"
            else {"prompt_tokens": 5_000, "completion_tokens": 2_000}
        )
        client, _ = wired(provider, [text_for(provider)("Paid for.", usage=heavy)])

        result = await ask(client, Recorder(), token_budget=6_000)

        assert result.text == "Paid for."

    async def test_a_tool_name_the_model_invented_is_never_logged(self, provider):
        invented = "balance_is_48201.15_at_LOBLAWS"
        client, _ = wired(provider, [calls_for(provider)(("c1", invented, {}))])

        with capture_logs() as logs, pytest.raises(LlmError) as raised:
            await ask(client, Recorder())

        assert invented not in json.dumps(logs, default=str)
        assert invented not in str(raised.value)
        calls = [entry for entry in logs if entry["event"] == "llm_call"]
        assert calls[0]["tools"] == ["<unregistered>"]

    async def test_a_turn_naming_an_unregistered_tool_runs_nothing(self, provider):
        client, _ = wired(
            provider,
            [
                calls_for(provider)(
                    ("c1", "spending_summary", GOOD_ARGS), ("c2", "drop_tables", {})
                )
            ],
        )
        recorder = Recorder()

        with pytest.raises(LlmError):
            await ask(client, recorder)
        assert recorder.runs == []

    async def test_two_malformed_calls_in_one_turn_still_get_a_correction(
        self, provider
    ):
        bad = {"from_month": "2016-01"}
        client, sent = wired(
            provider,
            [
                calls_for(provider)(
                    ("c1", "spending_summary", bad), ("c2", "spending_summary", bad)
                ),
                calls_for(provider)(("c3", "spending_summary", GOOD_ARGS)),
                text_for(provider)("Fixed."),
            ],
        )
        recorder = Recorder()

        result = await ask(client, recorder)

        assert result.text == "Fixed."
        assert recorder.runs == [("spending_summary", GOOD_ARGS)]
        # Both errors went back in the second request.
        assert json.dumps(sent[1]["messages"]).count("is required") == 2

    async def test_a_slow_handler_is_stopped_at_the_deadline(self, provider):
        client, _ = wired(provider, [calls_for(provider)(("c1", "goals", {}))])

        async def slow(args: dict) -> dict:
            await asyncio.sleep(5)
            return {}

        with pytest.raises(LlmError, match="deadline"):
            await run_tools(
                client,
                system="s",
                messages=[Message.user("q")],
                tools=[GOALS],
                handlers={"goals": slow},
                deadline_seconds=0.05,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gemini", "openrouter"])
async def test_chat_never_thinks_whatever_imports_are_set_to(provider):
    client, sent = wired(provider, [openai_text("Hi.")], llm_thinking_budget=8_192)

    await ask(client, Recorder(), max_output_tokens=400)

    body = sent[0]
    if provider == "openrouter":
        assert body["reasoning"] == {"effort": "none"}
    else:
        assert body["reasoning_effort"] == "none"
    assert body["max_tokens"] == 400, "nothing reserved for thinking"


@pytest.mark.asyncio
async def test_calls_with_no_cost_are_counted_so_a_partial_total_is_visible():
    client, _ = wired(
        "openrouter",
        [
            openai_text(
                "Hi.", usage={"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.01}
            ),
            openai_text("Hi.", usage={"prompt_tokens": 1, "completion_tokens": 1}),
        ],
    )

    await ask(client, Recorder())
    await ask(client, Recorder())

    assert client.usage.cost == pytest.approx(0.01)
    assert client.usage.uncosted_calls == 1


def test_truncating_ten_years_of_rows_is_fast_and_keeps_as_many_as_fit():
    rows = [
        {"merchant": "Amazon Marketplace", "amount": "12.99", "on": "2021-03-04"}
        for _ in range(36_000)
    ]

    started = time.perf_counter()
    text = tool_result({"rows": rows})
    elapsed = time.perf_counter() - started

    body = json.loads(text)
    # Generous for a slow CI runner; the old row-by-row cut took minutes here.
    assert elapsed < 2.0
    assert body["truncated"] is True and body["row_count"] == 36_000
    assert len(text.encode()) <= TOOL_RESULT_MAX_BYTES
    one_more = {**body, "rows": rows[: len(body["rows"]) + 1]}
    assert len(json.dumps(one_more, separators=(",", ":")).encode()) > (
        TOOL_RESULT_MAX_BYTES
    ), "the most rows that fit, not fewer"


def test_a_single_row_too_big_for_the_cap_says_so():
    body = json.loads(tool_result({"rows": [{"note": "x" * 5_000}]}, max_bytes=200))

    assert body == {"truncated": True, "row_count": 1, "error": "too large"}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
class TestBudgetRefusesARoundThatWouldNotFit:
    def heavy(self, provider: str) -> dict:
        return (
            {"input_tokens": 5_800, "output_tokens": 100}
            if provider == "anthropic"
            else {"prompt_tokens": 5_800, "completion_tokens": 100}
        )

    async def test_a_round_that_would_overshoot_is_never_started(self, provider):
        client, sent = wired(
            provider,
            [
                calls_for(provider)(("c1", "goals", {}), usage=self.heavy(provider)),
                text_for(provider)("Never asked."),
            ],
        )
        recorder = Recorder()

        with pytest.raises(LlmError, match="token budget"):
            await ask(client, recorder, token_budget=6_000)
        assert len(sent) == 1, "the second round was not started"
        assert recorder.runs == [], "nor its tools run"

    async def test_a_round_that_fits_goes_ahead(self, provider):
        client, _ = wired(
            provider,
            [
                calls_for(provider)(("c1", "goals", {}), usage=self.heavy(provider)),
                text_for(provider)("Answered."),
            ],
        )

        # 5,900 spent + at least 5,800 in + 400 out = 12,100.
        result = await ask(client, Recorder(), token_budget=12_100)

        assert result.text == "Answered."
