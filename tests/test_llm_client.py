"""Choosing a model provider is configuration, and has to stay that way.

M3 ships on a free tier against synthetic fixtures and must move to a paid,
no-training tier before a real statement is parsed (PRD Appendix A.3). These
tests exist so that swap stays an environment variable rather than a code change
someone has to find under deadline.
"""

from __future__ import annotations

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
        llm_model="gemini-2.0-flash",
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
