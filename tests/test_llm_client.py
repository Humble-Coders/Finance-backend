"""Choosing a model provider is configuration, and has to stay that way.

M3 ships on a free tier against synthetic fixtures and must move to a paid,
no-training tier before a real statement is parsed (PRD Appendix A.3). These
tests exist so that swap stays an environment variable rather than a code change
someone has to find under deadline.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.services import llm
from app.services.llm import LlmError, build_client, register_adapter


def settings(**overrides) -> Settings:
    base = dict(
        database_url="postgresql://localhost/x",
        supabase_url="https://example.supabase.co",
        llm_api_key="key",
        llm_provider="gemini",
        llm_model="gemini-2.5-flash",
    )
    return Settings(**{**base, **overrides})


class TestProviderSelection:
    def test_the_provider_setting_picks_the_adapter(self):
        seen = {}

        class FakeA:
            def __init__(self, s):
                seen["built"] = "a"

        class FakeB:
            def __init__(self, s):
                seen["built"] = "b"

        register_adapter("fake-a", FakeA)
        register_adapter("fake-b", FakeB)

        build_client(settings(llm_provider="fake-a"))
        assert seen["built"] == "a"

        build_client(settings(llm_provider="fake-b"))
        assert seen["built"] == "b"

    def test_openai_compatible_providers_share_one_adapter(self):
        """Gemini, OpenAI, Groq and OpenRouter all speak the same shape.

        Pinned because the swap this module exists for is between two of them.
        """
        adapters = {llm._ADAPTERS[name] for name in ("gemini", "openai", "groq")}
        assert len(adapters) == 1

    def test_anthropic_has_its_own_adapter(self):
        assert llm._ADAPTERS["anthropic"] is not llm._ADAPTERS["openai"]


class TestMisconfigurationSaysWhat:
    def test_an_unknown_provider_names_the_ones_that_exist(self):
        with pytest.raises(LlmError) as raised:
            build_client(settings(llm_provider="not-a-provider"))
        assert "anthropic" in str(raised.value)

    def test_a_missing_key_fails_before_any_request(self):
        with pytest.raises(LlmError, match="LLM_API_KEY"):
            build_client(settings(llm_api_key=""))

    def test_a_missing_model_fails_before_any_request(self):
        with pytest.raises(LlmError, match="LLM_MODEL"):
            build_client(settings(llm_model=""))


# ── What a call sends, and what it makes of the answer ──────────────────


def _answer(
    content: str | None = "[]",
    *,
    finish: str = "stop",
    usage: dict | None = None,
) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": usage if usage is not None else {},
    }


def wired(provider: str, respond: dict, *, budget: int = 2_048, model: str = "m"):
    """A real client whose connection is a fake, and the bodies it sent."""
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=respond)

    client = build_client(
        settings(llm_provider=provider, llm_model=model, llm_thinking_budget=budget)
    )
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, sent


async def complete(client, max_output_tokens: int = 8_000) -> str:
    return await client.complete(
        system="s", user="u", max_output_tokens=max_output_tokens
    )


@pytest.mark.asyncio
class TestThinkingCannotCrowdOutTheAnswer:
    """A thinking model's reasoning counts against the same `max_tokens` as the
    answer on OpenRouter. Unbounded, it could leave the answer too little room,
    and an answer cut off part way is JSON that does not parse."""

    async def test_openrouter_gets_a_budget_and_the_answer_keeps_its_room(self):
        client, sent = wired("openrouter", _answer(), budget=2_048)

        await complete(client, max_output_tokens=8_000)

        body = sent[0]
        assert body["reasoning"] == {"max_tokens": 2_048}
        assert body["max_tokens"] == 8_000 + 2_048, "the answer keeps all 8,000"

    async def test_a_budget_of_nothing_turns_thinking_off(self):
        client, sent = wired("openrouter", _answer(), budget=0)

        await complete(client, max_output_tokens=8_000)

        assert sent[0]["reasoning"] == {"effort": "none"}
        assert sent[0]["max_tokens"] == 8_000

    async def test_gemini_direct_gets_the_level_that_covers_the_budget(self):
        # Google's compatible endpoint takes a level; each is a fixed budget,
        # and what is reserved is that budget, not the setting.
        client, sent = wired("gemini", _answer(), budget=2_048)

        await complete(client, max_output_tokens=8_000)

        assert sent[0]["reasoning_effort"] == "medium"
        assert sent[0]["max_tokens"] == 8_000 + 8_192

    async def test_gemini_direct_with_no_budget_does_not_think(self):
        client, sent = wired("gemini", _answer(), budget=0)

        await complete(client, max_output_tokens=8_000)

        assert sent[0]["reasoning_effort"] == "none"
        assert sent[0]["max_tokens"] == 8_000

    async def test_a_provider_without_a_known_field_is_sent_none(self):
        # An unrecognised field is a 400 on some providers.
        client, sent = wired("groq", _answer(), budget=2_048)

        await complete(client, max_output_tokens=8_000)

        assert "reasoning" not in sent[0]
        assert "reasoning_effort" not in sent[0]
        assert sent[0]["max_tokens"] == 8_000

    async def test_a_negative_budget_is_read_as_none(self):
        client, sent = wired("openrouter", _answer(), budget=-5)

        await complete(client, max_output_tokens=8_000)

        assert sent[0]["reasoning"] == {"effort": "none"}


@pytest.mark.asyncio
class TestACutOffAnswerIsAFailure:
    """Before, a statement window whose answer ran out of room came back with
    no rows — logged as "not JSON" and otherwise silent — and every transaction
    in it was missing from the import."""

    async def test_running_out_of_room_raises(self):
        client, _ = wired("openrouter", _answer('[{"date": "2026-0', finish="length"))

        with pytest.raises(LlmError, match="cut off"):
            await complete(client)

    async def test_thinking_until_nothing_is_left_raises(self):
        # Every token spent reasoning: no content at all.
        client, _ = wired("openrouter", _answer(None, finish="length"))

        with pytest.raises(LlmError, match="cut off"):
            await complete(client)

    async def test_a_finished_answer_comes_back_whole(self):
        client, _ = wired("openrouter", _answer("[1, 2]", finish="stop"))
        assert await complete(client) == "[1, 2]"


@pytest.mark.asyncio
class TestEveryCallIsCounted:
    async def test_tokens_reasoning_and_cost_are_recorded(self):
        usage = {
            "prompt_tokens": 3_800,
            "completion_tokens": 2_500,
            "completion_tokens_details": {"reasoning_tokens": 600},
            "cost": 0.0074,
        }
        client, _ = wired("openrouter", _answer(usage=usage))

        await complete(client)
        await complete(client)

        assert client.usage.calls == 2
        assert client.usage.input_tokens == 7_600
        assert client.usage.output_tokens == 5_000
        assert client.usage.reasoning_tokens == 1_200
        assert client.usage.cost == pytest.approx(0.0148)

    async def test_openrouter_is_asked_to_report_its_cost(self):
        client, sent = wired("openrouter", _answer())
        await complete(client)
        assert sent[0]["usage"] == {"include": True}

    async def test_a_provider_that_reports_no_cost_is_not_given_one(self):
        # None, not a guess from a price list that changes.
        client, _ = wired(
            "gemini", _answer(usage={"prompt_tokens": 10, "completion_tokens": 5})
        )

        await complete(client)

        assert client.usage.cost is None
        assert client.usage.input_tokens == 10

    async def test_a_missing_usage_block_counts_the_call_and_nothing_else(self):
        client, _ = wired("groq", {"choices": [{"message": {"content": "[]"}}]})

        await complete(client)

        assert client.usage.calls == 1
        assert client.usage.input_tokens == 0

    async def test_the_count_carries_no_prompt_or_answer(self, capsys):
        # The prompt holds statement text. The log line may carry numbers only.
        client, _ = wired(
            "openrouter", _answer("SECRET-ANSWER", usage={"prompt_tokens": 1})
        )

        await client.complete(
            system="SECRET-SYSTEM", user="SECRET-USER", max_output_tokens=10
        )

        logged = capsys.readouterr().out
        assert "llm_call" in logged
        assert "SECRET" not in logged


@pytest.mark.asyncio
class TestOneConnectionPerClient:
    async def test_calls_go_through_the_clients_own_connection(self):
        # `_post` opened a new connection per call, ignoring the one it was
        # given. A fake connection only sees requests if they use it.
        client, sent = wired("openrouter", _answer())

        await complete(client)
        await complete(client)

        assert len(sent) == 2


@pytest.mark.asyncio
class TestAnthropic:
    def wired(self, respond: dict):
        sent: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            return httpx.Response(200, json=respond)

        client = build_client(settings(llm_provider="anthropic", llm_model="claude"))
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return client, sent

    async def test_a_call_reaches_the_provider(self):
        # It raised AttributeError before: there was no `_http` to post through.
        client, sent = self.wired(
            {
                "content": [{"text": "[]"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 9, "output_tokens": 2},
            }
        )

        assert await complete(client, max_output_tokens=100) == "[]"
        assert len(sent) == 1
        assert client.usage.input_tokens == 9

    async def test_running_out_of_room_raises(self):
        client, _ = self.wired(
            {"content": [{"text": "[{"}], "stop_reason": "max_tokens"}
        )

        with pytest.raises(LlmError, match="cut off"):
            await complete(client, max_output_tokens=100)
