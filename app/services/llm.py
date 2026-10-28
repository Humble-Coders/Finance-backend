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

**Tool calling** (#69) is how the AI coach answers from real data: `converse`
lets a model ask for a tool instead of answering, and `run_tools` runs the
registered handlers and feeds their results back until it answers. Built to be
cheap: the system prompt and tools go first and identical every time so the
provider can cache them, results are minified and capped (`tool_result`), and
the loop stops at a round limit, a deadline and a token budget. Tool arguments
and results are never logged — they carry a household's figures.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import structlog

from app.config import Settings

__all__ = [
    "AssistantTurn",
    "LlmClient",
    "LlmError",
    "Message",
    "RunResult",
    "ToolCall",
    "ToolError",
    "ToolSpec",
    "Usage",
    "build_client",
    "close_client",
    "register_adapter",
    "run_tools",
    "tool_result",
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
    # Every input token, cached or not.
    input_tokens: int = 0
    # Inside `input_tokens`: the part the provider served from its prompt cache,
    # billed at a discount. What prompt caching saves shows up here.
    cached_input_tokens: int = 0
    output_tokens: int = 0
    # Inside `output_tokens`, not on top of it: billed as output, but spent
    # thinking rather than answering. Reported separately because it is the
    # part a setting can turn down.
    reasoning_tokens: int = 0
    # In the provider's currency when it says (OpenRouter does). When it does
    # not, an estimate from `LLM_PRICES` if that model has an entry — and then
    # `cost_estimated` says so; otherwise None, never a silent guess.
    cost: float | None = None
    cost_estimated: bool = False

    def add(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
        cost: float | None,
        cached_input_tokens: int = 0,
        estimated: bool = False,
    ) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.cached_input_tokens += cached_input_tokens
        self.output_tokens += output_tokens
        self.reasoning_tokens += reasoning_tokens
        if cost is not None:
            self.cost = (self.cost or 0.0) + cost
            self.cost_estimated = self.cost_estimated or estimated


# --- Conversations with tools (#69) ---------------------------------------------


class ToolError(Exception):
    """Raised by a tool handler for input it cannot use — a date out of range,
    say. Its message goes back to the model as a tool error it can correct;
    anything else a handler raises is a bug and propagates."""


@dataclass(frozen=True)
class ToolSpec:
    """A tool the model may call: a name, what it is for, and a JSON schema
    for its arguments (objects with typed properties — see `_schema_problem`)."""

    name: str
    description: str
    parameters: dict


@dataclass(frozen=True)
class ToolCall:
    """A model asking for a tool. [arguments] is None when what it sent was
    not a JSON object."""

    id: str
    name: str
    arguments: dict | None


@dataclass(frozen=True)
class Message:
    """One turn of a conversation after the system prompt.

    `user` and `assistant` carry text; an `assistant` turn may carry tool calls
    instead of, or beside, text; a `tool` turn is one call's result.
    """

    role: str
    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    is_error: bool = False

    @staticmethod
    def user(text: str) -> Message:
        return Message("user", text=text)


@dataclass(frozen=True)
class AssistantTurn:
    """What the model said: final text, or tools it wants run first."""

    text: str | None
    tool_calls: tuple[ToolCall, ...] = ()

    @property
    def is_final(self) -> bool:
        return not self.tool_calls


@dataclass
class RunResult:
    """A finished `run_tools`: the answer, the whole exchange, and every tool
    result the answer could have used — what a grounding check reads."""

    text: str
    messages: list[Message]
    rounds: int
    tool_results: list[tuple[str, str]] = field(default_factory=list)


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

    async def converse(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        max_output_tokens: int,
    ) -> AssistantTurn:
        """One model turn of a conversation that may call [tools].

        The system prompt and the tools are sent first and unchanged on every
        call, so the provider can serve them from its prompt cache; only the
        messages after them vary.
        """


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
        self._prices = settings.llm_prices
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

        self._record_usage(data, choice, payload["max_tokens"], ())
        if choice.get("finish_reason") == "length" or content is None:
            raise _cut_off(self._model, payload["max_tokens"])
        return content

    def _record_usage(
        self, data: dict, choice: dict, max_tokens: int, tools: Sequence[str]
    ) -> None:
        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        prompt = usage.get("prompt_tokens_details") or {}
        _record(
            self.usage,
            model=self._model,
            input_tokens=usage.get("prompt_tokens"),
            cached_input_tokens=prompt.get("cached_tokens"),
            output_tokens=usage.get("completion_tokens"),
            reasoning_tokens=details.get("reasoning_tokens"),
            cost=usage.get("cost"),
            prices=self._prices,
            finish=choice.get("finish_reason"),
            max_tokens=max_tokens,
            tools=tools,
        )

    async def converse(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        max_output_tokens: int,
    ) -> AssistantTurn:
        thinking, reserved = self._thinking()
        payload: dict[str, Any] = {
            "model": self._model,
            # System first, tools next, and both identical every call: the
            # cacheable prefix. Gemini 2.5 caches a repeated prefix on its own.
            "messages": [{"role": "system", "content": system}]
            + [_openai_message(m) for m in messages],
            "max_tokens": max_output_tokens + reserved,
            "temperature": 0,
            **thinking,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        if self._provider == "openrouter":
            payload["usage"] = {"include": True}
        data = await _post(
            self._http,
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._key}"},
            payload=payload,
        )
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError("unexpected response shape") from exc
        calls = tuple(
            ToolCall(
                id=str(raw.get("id") or f"call_{i}"),
                name=str((raw.get("function") or {}).get("name") or ""),
                arguments=_json_object((raw.get("function") or {}).get("arguments")),
            )
            for i, raw in enumerate(message.get("tool_calls") or [])
        )
        self._record_usage(data, choice, payload["max_tokens"], [c.name for c in calls])
        if choice.get("finish_reason") == "length":
            raise _cut_off(self._model, payload["max_tokens"])
        text = message.get("content")
        if not calls and not text:
            raise LlmError("the model returned neither text nor a tool call")
        return AssistantTurn(text=text or None, tool_calls=calls)


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
        self._prices = settings.llm_prices
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

        self._record_usage(data, max_output_tokens, ())
        if data.get("stop_reason") == "max_tokens":
            raise _cut_off(self._model, max_output_tokens)
        return text

    def _record_usage(self, data: dict, max_tokens: int, tools: Sequence[str]) -> None:
        usage = data.get("usage") or {}

        def n(key: str) -> int:
            value = usage.get(key)
            return value if isinstance(value, int) and value >= 0 else 0

        # Anthropic counts cache reads and writes apart from `input_tokens`;
        # summed, they are every token of input, as the other providers count.
        cached = n("cache_read_input_tokens")
        _record(
            self.usage,
            model=self._model,
            input_tokens=n("input_tokens") + cached + n("cache_creation_input_tokens"),
            cached_input_tokens=cached,
            output_tokens=usage.get("output_tokens"),
            reasoning_tokens=0,
            cost=None,
            prices=self._prices,
            finish=data.get("stop_reason"),
            max_tokens=max_tokens,
            tools=tools,
        )

    async def converse(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        max_output_tokens: int,
    ) -> AssistantTurn:
        specs = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]
        if specs:
            # The cache breakpoint on the last tool covers the system prompt and
            # every tool before it: the whole stable prefix.
            specs[-1] = {**specs[-1], "cache_control": {"type": "ephemeral"}}
        payload: dict[str, Any] = {
            "model": self._model,
            "system": [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": _anthropic_messages(messages),
            "max_tokens": max_output_tokens,
            "temperature": 0,
        }
        if specs:
            payload["tools"] = specs
        data = await _post(
            self._http,
            f"{self._base_url}/messages",
            headers={"x-api-key": self._key, "anthropic-version": "2023-06-01"},
            payload=payload,
        )
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise LlmError("unexpected response shape")
        texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        calls = tuple(
            ToolCall(
                id=str(b.get("id") or f"call_{i}"),
                name=str(b.get("name") or ""),
                arguments=b.get("input") if isinstance(b.get("input"), dict) else None,
            )
            for i, b in enumerate(blocks)
            if b.get("type") == "tool_use"
        )
        self._record_usage(data, max_output_tokens, [c.name for c in calls])
        if data.get("stop_reason") == "max_tokens":
            raise _cut_off(self._model, max_output_tokens)
        text = "".join(texts).strip()
        if not calls and not text:
            raise LlmError("the model returned neither text nor a tool call")
        return AssistantTurn(text=text or None, tool_calls=calls)


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
    cached_input_tokens: object = 0,
    prices: Mapping[str, Mapping[str, float]] | None = None,
    tools: Sequence[str] = (),
) -> None:
    """One line per call, and the running total.

    Counts and tool **names** only — never the prompt, the answer, or a tool's
    arguments or result, which hold statement text and a household's figures.
    The cost is the provider's when it reports one; otherwise an estimate from
    [prices] when the model has an entry, marked as estimated.
    """

    def count(value: object) -> int:
        return value if isinstance(value, int) and value >= 0 else 0

    spent = (
        cost if isinstance(cost, int | float) and not isinstance(cost, bool) else None
    )
    estimated = False
    if spent is None:
        spent = estimate_cost(
            (prices or {}).get(model),
            input_tokens=count(input_tokens),
            cached_input_tokens=count(cached_input_tokens),
            output_tokens=count(output_tokens),
        )
        estimated = spent is not None
    usage.add(
        input_tokens=count(input_tokens),
        cached_input_tokens=count(cached_input_tokens),
        output_tokens=count(output_tokens),
        reasoning_tokens=count(reasoning_tokens),
        cost=float(spent) if spent is not None else None,
        estimated=estimated,
    )
    log.info(
        "llm_call",
        model=model,
        input_tokens=count(input_tokens),
        cached_input_tokens=count(cached_input_tokens),
        output_tokens=count(output_tokens),
        reasoning_tokens=count(reasoning_tokens),
        cost=spent,
        cost_estimated=estimated,
        finish=finish if isinstance(finish, str) else None,
        max_tokens=max_tokens,
        tools=list(tools),
    )


def estimate_cost(
    price: Mapping[str, float] | None,
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> float | None:
    """What a call cost by [price] — dollars per million tokens for `input`,
    `cached_input` (input when absent) and `output` — or None without a price."""
    if not price or "input" not in price or "output" not in price:
        return None
    cached = min(cached_input_tokens, input_tokens)
    per = 1_000_000
    return (
        (input_tokens - cached) * price["input"]
        + cached * price.get("cached_input", price["input"])
        + output_tokens * price["output"]
    ) / per


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


# --- Shaping messages for each provider ------------------------------------------


def _openai_message(message: Message) -> dict:
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.text or "",
        }
    if message.role == "assistant" and message.tool_calls:
        return {
            "role": "assistant",
            "content": message.text,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments or {}),
                    },
                }
                for call in message.tool_calls
            ],
        }
    return {"role": message.role, "content": message.text or ""}


def _anthropic_messages(messages: Sequence[Message]) -> list[dict]:
    """Anthropic's shape: tool calls are `tool_use` blocks in the assistant turn,
    and every result of one turn goes back together in a single user message."""
    out: list[dict] = []
    for message in messages:
        if message.role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": message.text or "",
            }
            if message.is_error:
                block["is_error"] = True
            last = out[-1] if out else None
            if (
                last
                and last["role"] == "user"
                and isinstance(last["content"], list)
                and all(b.get("type") == "tool_result" for b in last["content"])
            ):
                last["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif message.role == "assistant" and message.tool_calls:
            content: list[dict] = []
            if message.text:
                content.append({"type": "text", "text": message.text})
            content += [
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments or {},
                }
                for call in message.tool_calls
            ]
            out.append({"role": "assistant", "content": content})
        else:
            out.append({"role": message.role, "content": message.text or ""})
    return out


def _json_object(raw: object) -> dict | None:
    """Tool arguments as an object, or None when they are not one."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# --- Tool results and the loop -----------------------------------------------------

# What one tool result may weigh when it goes back to the model. Tools return
# summaries (#83); this is the backstop that keeps a long list from being sent.
TOOL_RESULT_MAX_BYTES = 4_096


def tool_result(obj: object, max_bytes: int = TOOL_RESULT_MAX_BYTES) -> str:
    """[obj] as compact JSON for the model — no indentation, no spaces — and
    never more than [max_bytes].

    Over the cap, rows are dropped from the end of its `rows` list (a bare list
    counts as rows) and `truncated` is set, with `row_count` kept as the true
    count, so the model knows it holds part of the answer and can say so.
    """

    def dump(value: object) -> str:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)

    text = dump(obj)
    if len(text.encode()) <= max_bytes:
        return text
    body = dict(obj) if isinstance(obj, Mapping) else {"rows": obj}
    rows = body.get("rows")
    if isinstance(rows, list):
        body.setdefault("row_count", len(rows))
        body["truncated"] = True
        kept = list(rows)
        while kept:
            kept.pop()
            body["rows"] = kept
            text = dump(body)
            if len(text.encode()) <= max_bytes:
                return text
    return dump(
        {"truncated": True, "row_count": body.get("row_count"), "error": "too large"}
    )


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _schema_problem(
    value: object, schema: Mapping[str, Any], path: str = ""
) -> str | None:
    """Why [value] does not fit [schema], or None.

    The subset tool schemas use: `type`, `properties`, `required`,
    `additionalProperties: false`, `enum`, `items`, `minimum` / `maximum`.
    Enough to send a malformed call back to the model with a reason, without a
    dependency for it.
    """
    where = path or "arguments"
    kind = schema.get("type")
    if kind in _JSON_TYPES:
        allowed = _JSON_TYPES[kind]
        if not isinstance(value, allowed) or (
            kind in ("integer", "number") and isinstance(value, bool)
        ):
            return f"{where} must be a {kind}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{where} must be one of {schema['enum']}"
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{where} must be at least {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{where} must be at most {schema['maximum']}"
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                return f"{where}.{name} is required"
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                return f"{where} has unknown fields {extra}"
        for name, item in value.items():
            if name in properties:
                problem = _schema_problem(item, properties[name], f"{where}.{name}")
                if problem:
                    return problem
    if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        for i, item in enumerate(value):
            problem = _schema_problem(item, schema["items"], f"{where}[{i}]")
            if problem:
                return problem
    return None


Handler = Callable[[dict], Awaitable[object]]


async def run_tools(
    client: LlmClient,
    *,
    system: str,
    messages: Sequence[Message],
    tools: Sequence[ToolSpec],
    handlers: Mapping[str, Handler],
    max_output_tokens: int = 400,
    max_rounds: int = 3,
    deadline_seconds: float = 45.0,
    token_budget: int | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RunResult:
    """Talk to the model until it answers, running the tools it asks for.

    Stops with `LlmError` at [max_rounds], at the deadline, once [token_budget]
    (input plus output, all rounds) is spent, or when the model asks for a tool
    that is not in [handlers] — which are never bypassed. Arguments that are
    not a JSON object, or do not fit the tool's schema, go back to the model
    once as a tool error; a second malformed call ends the run. A handler's
    `ToolError` goes back as a tool error too; anything else it raises
    propagates.
    """
    specs = {spec.name: spec for spec in tools}
    unknown = set(handlers) - set(specs)
    if unknown:
        raise ValueError(f"handlers without a tool spec: {sorted(unknown)}")
    transcript = list(messages)
    results: list[tuple[str, str]] = []
    usage = getattr(client, "usage", None)
    spent_before = (usage.input_tokens + usage.output_tokens) if usage else 0
    started = clock()
    malformed = 0

    for round_number in range(1, max_rounds + 1):
        remaining = deadline_seconds - (clock() - started)
        if remaining <= 0:
            raise LlmError("no answer before the deadline")
        try:
            turn = await asyncio.wait_for(
                client.converse(
                    system=system,
                    messages=transcript,
                    tools=tools,
                    max_output_tokens=max_output_tokens,
                ),
                timeout=remaining,
            )
        except TimeoutError as exc:
            raise LlmError("no answer before the deadline") from exc
        if usage is not None and token_budget is not None:
            spent = usage.input_tokens + usage.output_tokens - spent_before
            if spent > token_budget:
                log.warning("llm_token_budget_spent", spent=spent, budget=token_budget)
                raise LlmError("the question's token budget is spent")
        if turn.is_final:
            return RunResult(
                text=turn.text or "",
                messages=transcript + [Message("assistant", text=turn.text)],
                rounds=round_number,
                tool_results=results,
            )

        transcript.append(
            Message("assistant", text=turn.text, tool_calls=turn.tool_calls)
        )
        for call in turn.tool_calls:
            if call.name not in handlers:
                raise LlmError(
                    f"the model asked for an unregistered tool {call.name!r}"
                )
            problem = (
                "arguments must be a JSON object"
                if call.arguments is None
                else _schema_problem(call.arguments, specs[call.name].parameters)
            )
            if problem is not None:
                malformed += 1
                if malformed > 1:
                    raise LlmError("the model sent malformed tool arguments twice")
                transcript.append(
                    Message("tool", text=problem, tool_call_id=call.id, is_error=True)
                )
                continue
            try:
                result = tool_result(await handlers[call.name](call.arguments))
            except ToolError as error:
                transcript.append(
                    Message(
                        "tool", text=str(error), tool_call_id=call.id, is_error=True
                    )
                )
                continue
            results.append((call.name, result))
            transcript.append(Message("tool", text=result, tool_call_id=call.id))

    raise LlmError(f"no answer within {max_rounds} rounds")
