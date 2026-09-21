"""The one place the application talks to a language model.

Two things this module exists to contain.

**Provider choice is configuration, not code.** The PRD leaves the provider open
(OD2), and M3 deliberately starts on a *free* tier while the only data is
synthetic, swapping to a paid no-training tier before any real statement is
parsed (PRD Appendix A.3, `docs/ROADMAP.md` → Before launch). That swap has to be
an environment variable, or it will not happen on the day it has to.

**Nothing else in the codebase may call a model.** Every prompt, timeout, retry
and redaction obligation lives behind `LlmClient`, so there is exactly one place
to audit when someone asks what we send to whom.

The transport is OpenAI-compatible chat completions, which Google's Gemini,
OpenAI, Groq, Mistral and OpenRouter all speak; Anthropic has its own shape and
its own adapter. Adding a provider is a class and a registry entry.
"""

from __future__ import annotations

from typing import Protocol

import httpx
import structlog

from app.config import Settings

__all__ = [
    "LlmClient",
    "LlmError",
    "build_client",
    "close_client",
    "register_adapter",
]

log = structlog.get_logger()

# A model that has not answered in a minute is not about to. The caller holds an
# HTTP request open while this runs, so the budget is the user's patience.
DEFAULT_TIMEOUT_SECONDS = 60.0


class LlmError(Exception):
    """The model could not be reached, or would not answer.

    Deliberately one exception rather than a hierarchy: every caller does the
    same thing with it (fail the import, say so, let the user retry with the
    file they still have), so distinguishing a timeout from a 500 would only
    create branches nobody takes.
    """


class LlmClient(Protocol):
    """What the rest of the application is allowed to know about a model."""

    async def aclose(self) -> None:
        """Release the connection. Callers use `close_client`, which tolerates
        a client that has none."""

    @property
    def model(self) -> str:
        """The model actually answering, recorded with anything it produces.

        A bad parse has to be traceable to what produced it. "The model was
        wrong" is not actionable when nobody can say which model.
        """

    async def complete(self, *, system: str, user: str, max_output_tokens: int) -> str:
        """One turn in, one turn out. No conversation state, by design."""


class _OpenAICompatibleClient:
    """Chat completions as OpenAI defined them, which most providers now speak."""

    def __init__(self, settings: Settings) -> None:
        self._key = settings.llm_api_key
        self._model = settings.llm_model
        self._base_url = settings.llm_base_url.rstrip("/")
        # One connection for the whole parse. A long statement is many calls to
        # the same host, and a fresh client per call is a fresh TLS handshake
        # per call — pure latency on a path the user is waiting through.
        self._http = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)

    @property
    def model(self) -> str:
        return self._model

    async def aclose(self) -> None:
        await self._http.aclose()

    async def complete(self, *, system: str, user: str, max_output_tokens: int) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_output_tokens,
            # Reading a table is not a creative task. Two runs over the same
            # statement should not disagree about what it says.
            "temperature": 0,
        }
        data = await _post(
            self._http,
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._key}"},
            payload=payload,
        )
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError("unexpected response shape") from exc


class _AnthropicClient:
    """Anthropic's Messages API, which puts the system prompt beside the turns."""

    def __init__(self, settings: Settings) -> None:
        self._key = settings.llm_api_key
        self._model = settings.llm_model
        self._base_url = (
            settings.llm_base_url or "https://api.anthropic.com/v1"
        ).rstrip("/")

    @property
    def model(self) -> str:
        return self._model

    async def complete(self, *, system: str, user: str, max_output_tokens: int) -> str:
        payload = {
            "model": self._model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "max_tokens": max_output_tokens,
            "temperature": 0,
        }
        data = await _post(
            self._http,
            f"{self._base_url}/messages",
            headers={"x-api-key": self._key, "anthropic-version": "2023-06-01"},
            payload=payload,
        )
        try:
            return data["content"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError("unexpected response shape") from exc


async def _post(
    http: httpx.AsyncClient, url: str, *, headers: dict[str, str], payload: dict
) -> dict:
    """One request, with the failure modes flattened into `LlmError`.

    No retry here. A retry belongs to the caller that knows what the request
    cost and whether repeating it is safe — and for a statement parse it is the
    user's wait, not a background job's, so the answer is usually no.
    """
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                headers={**headers, "content-type": "application/json"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        # The URL and headers can carry a key; the payload carries statement
        # text. Neither goes in the log — the exception type is the diagnosis.
        log.warning("llm_request_failed", error=type(exc).__name__)
        raise LlmError("request failed") from exc

    if response.status_code >= 400:
        log.warning("llm_request_rejected", status=response.status_code)
        raise LlmError(f"provider returned {response.status_code}")

    try:
        return response.json()
    except ValueError as exc:
        raise LlmError("response was not JSON") from exc


_ADAPTERS: dict[str, type] = {
    # Everything OpenAI-compatible shares one adapter; the base URL is what
    # differs, and that is configuration.
    "openai": _OpenAICompatibleClient,
    "gemini": _OpenAICompatibleClient,
    "groq": _OpenAICompatibleClient,
    "openrouter": _OpenAICompatibleClient,
    "anthropic": _AnthropicClient,
}


def register_adapter(provider: str, factory: type) -> None:
    """Add a provider. Tests use this to install a fake under a real name."""
    _ADAPTERS[provider] = factory


def build_client(settings: Settings) -> LlmClient:
    """The configured client, or a clear error naming what is misconfigured."""
    factory = _ADAPTERS.get(settings.llm_provider)
    if factory is None:
        raise LlmError(
            f"unknown LLM provider {settings.llm_provider!r}; "
            f"known: {', '.join(sorted(_ADAPTERS))}"
        )
    if not settings.llm_api_key:
        raise LlmError("LLM_API_KEY is not set")
    if not settings.llm_model:
        raise LlmError("LLM_MODEL is not set")
    return factory(settings)


async def close_client(client: object) -> None:
    """Release a client's connection, if it holds one.

    Tolerant by design: test fakes are plain objects with a `complete`, and
    making every one of them implement a teardown it does not need would be a
    tax on writing tests, which is a tax on writing them at all.
    """
    aclose = getattr(client, "aclose", None)
    if aclose is not None:
        await aclose()
