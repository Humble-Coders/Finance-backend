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

from dataclasses import dataclass
from typing import Protocol

import httpx
import structlog

from app.config import Settings

__all__ = [
    "LlmClient",
    "LlmError",
    "Usage",
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


@dataclass
class Usage:
    """What a client's calls have cost so far, summed over its life.

    Read by a caller that wants one line per job — "this import cost N tokens"
    — rather than one per call. A fake client need not have it; callers read it
    with `getattr`.
    """

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Inside `output_tokens`, not on top of it: billed as output, but spent
    # thinking rather than answering. Reported separately because it is the
    # part a setting can turn down.
    reasoning_tokens: int = 0
    # In the provider's currency when it says (OpenRouter does); None when it
    # does not, rather than a guess from a price list that changes.
    cost: float | None = None

    def add(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
        cost: float | None,
    ) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.reasoning_tokens += reasoning_tokens
        if cost is not None:
            self.cost = (self.cost or 0.0) + cost


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
        self._provider = settings.llm_provider
        self._thinking_budget = max(settings.llm_thinking_budget, 0)
        self._base_url = settings.llm_base_url.rstrip("/")
        # One connection for the whole parse. A long statement is many calls to
        # the same host, and a fresh client per call is a fresh TLS handshake
        # per call — pure latency on a path the user is waiting through.
        self._http = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
        self.usage = Usage()

    @property
    def model(self) -> str:
        return self._model

    async def aclose(self) -> None:
        await self._http.aclose()

    def _thinking(self) -> tuple[dict, int]:
        """The request fields that bound a model's thinking, and the tokens to
        reserve for it on top of the answer.

        A thinking model spends tokens reasoning before it answers, billed as
        output, and on OpenRouter those count against the same `max_tokens` as
        the answer itself. Unbounded, a long think can leave too little room for
        the answer, and an answer cut off mid-JSON loses every row it held. So
        the budget is stated explicitly and added to `max_tokens`: the answer
        keeps the whole allowance its caller asked for.

        Sent only to providers whose field we know. Anyone else gets nothing, as
        before — an unrecognised field is a 400 on some of them.
        """
        budget = self._thinking_budget
        if self._provider == "openrouter":
            if budget == 0:
                return {"reasoning": {"effort": "none"}}, 0
            return {"reasoning": {"max_tokens": budget}}, budget
        if self._provider == "gemini":
            # Google's compatible endpoint takes a level, not a count; each
            # level is a fixed budget, and that is what is reserved.
            for effort, reserved in _GEMINI_EFFORTS:
                if budget <= reserved:
                    return {"reasoning_effort": effort}, reserved
            effort, reserved = _GEMINI_EFFORTS[-1]
            return {"reasoning_effort": effort}, reserved
        return {}, 0

    async def complete(self, *, system: str, user: str, max_output_tokens: int) -> str:
        thinking, reserved = self._thinking()
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_output_tokens + reserved,
            # Reading a table is not a creative task. Two runs over the same
            # statement should not disagree about what it says.
            "temperature": 0,
            **thinking,
        }
        if self._provider == "openrouter":
            # Asks for the call's cost in the response, so it is recorded as
            # billed rather than estimated from a price list.
            payload["usage"] = {"include": True}
        data = await _post(
            self._http,
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._key}"},
            payload=payload,
        )
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError("unexpected response shape") from exc

        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        _record(
            self.usage,
            model=self._model,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            reasoning_tokens=details.get("reasoning_tokens"),
            cost=usage.get("cost"),
            finish=choice.get("finish_reason"),
            max_tokens=payload["max_tokens"],
        )
        if choice.get("finish_reason") == "length" or content is None:
            raise _cut_off(self._model, payload["max_tokens"])
        return content


# Google's levels for 2.5 Flash and the budgets they stand for, smallest first.
# "none" turns thinking off, which 2.5 Flash allows and 2.5 Pro does not.
_GEMINI_EFFORTS = (("none", 0), ("low", 1_024), ("medium", 8_192), ("high", 24_576))


class _AnthropicClient:
    """Anthropic's Messages API, which puts the system prompt beside the turns."""

    def __init__(self, settings: Settings) -> None:
        self._key = settings.llm_api_key
        self._model = settings.llm_model
        self._base_url = (
            settings.llm_base_url or "https://api.anthropic.com/v1"
        ).rstrip("/")
        # Was missing: `complete` posted through `self._http`, so the first
        # call raised AttributeError rather than reaching Anthropic.
        self._http = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
        self.usage = Usage()

    @property
    def model(self) -> str:
        return self._model

    async def aclose(self) -> None:
        await self._http.aclose()

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
            text = data["content"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError("unexpected response shape") from exc

        usage = data.get("usage") or {}
        _record(
            self.usage,
            model=self._model,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            reasoning_tokens=0,
            cost=None,
            finish=data.get("stop_reason"),
            max_tokens=max_output_tokens,
        )
        if data.get("stop_reason") == "max_tokens":
            raise _cut_off(self._model, max_output_tokens)
        return text


def _record(
    usage: Usage,
    *,
    model: str,
    input_tokens: object,
    output_tokens: object,
    reasoning_tokens: object,
    cost: object,
    finish: object,
    max_tokens: int,
) -> None:
    """One line per call, and the running total.

    Counts only — never the prompt or the answer, which hold statement text.
    """

    def count(value: object) -> int:
        return value if isinstance(value, int) and value >= 0 else 0

    spent = (
        cost if isinstance(cost, int | float) and not isinstance(cost, bool) else None
    )
    usage.add(
        input_tokens=count(input_tokens),
        output_tokens=count(output_tokens),
        reasoning_tokens=count(reasoning_tokens),
        cost=float(spent) if spent is not None else None,
    )
    log.info(
        "llm_call",
        model=model,
        input_tokens=count(input_tokens),
        output_tokens=count(output_tokens),
        reasoning_tokens=count(reasoning_tokens),
        cost=spent,
        finish=finish if isinstance(finish, str) else None,
        max_tokens=max_tokens,
    )


def _cut_off(model: str, max_tokens: int) -> LlmError:
    """An answer that ran out of room, as a failure rather than a short answer.

    Treated as an error on purpose. The answer is JSON, and JSON cut off part
    way through does not parse, so before this a statement window that ran out
    of room came back with no rows at all, logged as "not JSON" and otherwise
    silent: every transaction in it was missing from the import and nothing
    said so. A failed import tells the person, costs no quota, and can be
    tried again; a quietly short one is wrong money on their dashboard.
    """
    log.warning("llm_output_truncated", model=model, max_tokens=max_tokens)
    return LlmError("the answer was cut off at the token limit")


async def _post(
    http: httpx.AsyncClient, url: str, *, headers: dict[str, str], payload: dict
) -> dict:
    """One request, with the failure modes flattened into `LlmError`.

    No retry here. A retry belongs to the caller that knows what the request
    cost and whether repeating it is safe — and for a statement parse it is the
    user's wait, not a background job's, so the answer is usually no.
    """
    try:
        # Through the client's own connection. This opened a new one per call,
        # ignoring `http`, which undid the reuse the clients set up for a long
        # statement's many calls.
        response = await http.post(
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
