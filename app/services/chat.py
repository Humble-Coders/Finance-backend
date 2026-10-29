"""The AI coach's conversation (#71): one question in, one grounded reply out.

The cheapest answer always wins, and the gates come before any model call:

1. **A suggestion chip** (`suggestion_id`) — #84's templated answer.
2. **A guardrail** — a request for a securities pick, or a prompt-injection
   attempt, gets a fixed reply; the model never sees it.
3. **The answer cache** — the same standalone question over the same ledger
   (`ledger_version`, #83) returns what the model said last time.
   None of these three call a model, need AI consent, or count against the
   daily cap.
4. **The model** — only past AI consent (409), the no-training tier in
   production (503) and the daily cap (429). `llm.run_tools` with the fixed
   tools first (#83's, then the app's screens) and #70's `run_sql` last; the
   last six turns as text; a short answer and #69's token budget.

Then the reply is checked: every figure must appear in that turn's tool
results (`chat_grounding`) — one retry naming the strays, then an honest
fallback; a truncated result must be described as a summary; a reply that
picks a security is replaced; a forward-looking one sets `disclaimer`.

Logged: one `chat_turn` line — how it was answered, rounds, tools, tokens,
cost, outcome. Never the question, the reply or a tool result.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.derived import ChatAnswerCache, ChatConversation, ChatDailyUsage
from app.services import (
    chat_service_tools,
    chat_sql,
    chat_templates,
    chat_tools,
    rollups,
)
from app.services.ai_consent import CONSENT_REQUIRED, current_policy, has_consented
from app.services.capabilities import currency_for
from app.services.chat_grounding import figures_in, ungrounded
from app.services.chat_guardrails import (
    DECLINE,
    FALLBACK,
    REFUSAL,
    TRUNCATION_NOTE,
    declined,
    forward_looking,
    normalized_hash,
    picks_a_security,
    refused,
    stands_alone,
    system_prompt,
)
from app.services.conflicts import log_conflict
from app.services.identity import ResolvedIdentity
from app.services.llm import LlmError, Message, build_client, close_client, run_tools

__all__ = [
    "CHAT_LIMIT_REACHED",
    "ChatRefused",
    "Reply",
    "forget_untouched",
    "post_message",
]

log = structlog.get_logger()

HISTORY_MESSAGES = 6
MAX_OUTPUT_TOKENS = 400
RETENTION = timedelta(days=365)
TITLE_CHARS = 60
PREVIEW_CHARS = 140

CHAT_LIMIT_REACHED = "chat_limit_reached"
TIER_NOT_CONFIRMED = "ai_processing_unavailable"
CHAT_UNAVAILABLE = "chat_unavailable"
NOTHING_TO_SAY = "There's nothing to answer that with yet."
UNAVAILABLE = "I couldn't answer that just now. Please try again in a moment."


class ChatRefused(Exception):
    """A question the server will not send to a model, and why."""

    def __init__(self, status: int, code: str, message: str, **extra) -> None:
        super().__init__(code)
        self.status, self.code, self.message, self.extra = status, code, message, extra


@dataclass
class Reply:
    text: str
    disclaimer: bool
    answered_by: str  # template | cache | model
    tools: list[dict] = field(default_factory=list)
    figures: list[str] = field(default_factory=list)


@dataclass
class _Run:
    """What the model run cost, for the `chat_turn` line."""

    rounds: int = 0
    tools: list[str] = field(default_factory=list)
    outcome: str = "ok"
    usage: object | None = None
    # LLM_CHAT_TOKEN_BUDGET, for each of the (at most two) runs.
    budget: int | None = None


async def forget_untouched(
    session: AsyncSession, household_id: uuid.UUID, now: datetime
) -> int:
    """Delete this household's conversations nobody has touched for a year —
    run when the list is opened, so no worker is needed."""
    result = await session.execute(
        delete(ChatConversation).where(
            ChatConversation.household_id == household_id,
            ChatConversation.updated_at < now - RETENTION,
        )
    )
    return result.rowcount or 0


async def post_message(
    session: AsyncSession,
    identity: ResolvedIdentity,
    conversation: ChatConversation,
    text: str,
    suggestion_id: str | None,
    *,
    settings: Settings,
    today: date,
    now: datetime | None = None,
) -> Reply:
    """Answer [text] (or the suggestion [suggestion_id]) in [conversation],
    store both turns, and return the reply. Raises `ChatRefused`."""
    now = now or datetime.now(UTC)
    household = identity.household
    currency = await currency_for(session, household)
    history = list(conversation.messages or [])
    run = _Run()

    if suggestion_id is not None:
        reply, question = await _templated(session, household, suggestion_id, today)
        text = text or question
    elif picks_a_security(text):
        reply, run.outcome = Reply(REFUSAL, False, "template"), "refused"
    elif declined(text):
        reply, run.outcome = Reply(DECLINE, False, "template"), "declined"
    else:
        first = not any(m.get("role") == "user" for m in history)
        cacheable = first or stands_alone(text)
        version = await rollups.ledger_version(session, household.id)
        reply = (
            await _cached(session, household.id, text, today, version, now)
            if cacheable
            else None
        )
        if reply is None:
            await _gates(session, identity, settings, today)
            reply = await _ask_model(
                session, identity, history, text, currency, settings, today, run
            )
            await _count(session, household.id, today)
            if cacheable and reply.text not in (FALLBACK, UNAVAILABLE):
                await _remember(session, household.id, text, today, version, reply, now)

    # Re-read under a row lock, and append to what is there *now*: a second
    # message sent to this conversation while the model was answering (a
    # double tap, a second device) has been stored meanwhile, and writing
    # back the list read at the start would erase it. The lock is held only
    # from here to the commit — never across the model call.
    await session.refresh(conversation, with_for_update=True)
    _store(conversation, list(conversation.messages or []), text, reply, now)
    _log(household.id, reply, run)
    return reply


# --- The short-cuts ---------------------------------------------------------------


async def _templated(session, household, suggestion_id, today) -> tuple[Reply, str]:
    try:
        answer = await chat_templates.answer(session, household, suggestion_id, today)
    except chat_templates.UnknownSuggestion:
        raise ChatRefused(404, "not_found", "No such suggestion.") from None
    question = next(s.text for s in chat_templates.SUGGESTIONS if s.id == suggestion_id)
    if answer is None:
        return Reply(NOTHING_TO_SAY, False, "template"), question
    return Reply(answer.text, False, "template", figures=list(answer.figures)), question


async def _cached(session, household_id, text, today, version, now) -> Reply | None:
    row = await session.scalar(
        select(ChatAnswerCache).where(
            ChatAnswerCache.household_id == household_id,
            ChatAnswerCache.question_hash == normalized_hash(text, today),
            ChatAnswerCache.ledger_version == version,
            ChatAnswerCache.expires_at > now,
        )
    )
    if row is None:
        return None
    answer = row.answer
    return Reply(
        answer["text"],
        bool(answer.get("disclaimer")),
        "cache",
        tools=list(answer.get("tools") or []),
        figures=list(answer.get("figures") or []),
    )


async def _remember(
    session, household_id, text, today, version, reply: Reply, now
) -> None:
    """Keep [reply] for the rest of [today] — the day is in the key, so it
    could not be found after midnight anyway — and clear the household's
    expired entries, so the table holds at most a day of answers."""
    await session.execute(
        delete(ChatAnswerCache).where(
            ChatAnswerCache.household_id == household_id,
            ChatAnswerCache.expires_at <= now,
        )
    )
    await session.execute(
        pg_insert(ChatAnswerCache)
        .values(
            id=uuid.uuid4(),
            household_id=household_id,
            question_hash=normalized_hash(text, today),
            ledger_version=version,
            answer={
                "text": reply.text,
                "disclaimer": reply.disclaimer,
                "tools": reply.tools,
                "figures": reply.figures,
            },
            expires_at=datetime.combine(today + timedelta(days=1), time.min, UTC),
        )
        .on_conflict_do_nothing()
    )


# --- The gates --------------------------------------------------------------------


async def _gates(session, identity: ResolvedIdentity, settings: Settings, today):
    policy = await current_policy(session)
    if policy is None or not await has_consented(session, identity.user, policy):
        log_conflict(
            CONSENT_REQUIRED,
            "no_ai_policy_configured" if policy is None else "consent_not_given",
            user_id=str(identity.user.id),
        )
        raise ChatRefused(
            409,
            CONSENT_REQUIRED,
            "Consent to AI processing is needed to ask your own questions.",
            policy_version=policy.version if policy else None,
        )
    if settings.is_production and not settings.llm_no_training_tier:
        # As statement import: the consent screen says the provider may not
        # train on this data, and until LLM_NO_TRAINING_TIER says so nothing
        # makes that true.
        log.error("llm_tier_not_confirmed", provider=settings.llm_provider)
        raise ChatRefused(503, TIER_NOT_CONFIRMED, "The AI coach is unavailable.")
    used = await session.scalar(
        select(ChatDailyUsage.model_answers).where(
            ChatDailyUsage.household_id == identity.household.id,
            ChatDailyUsage.day == today,
        )
    )
    if (used or 0) >= settings.chat_daily_message_limit:
        raise ChatRefused(
            429,
            CHAT_LIMIT_REACHED,
            "That's today's questions. Suggested questions still work.",
            limit=settings.chat_daily_message_limit,
        )


async def _count(session, household_id, today) -> None:
    statement = pg_insert(ChatDailyUsage).values(
        id=uuid.uuid4(), household_id=household_id, day=today, model_answers=1
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[ChatDailyUsage.household_id, ChatDailyUsage.day],
            set_={"model_answers": ChatDailyUsage.model_answers + 1},
        )
    )


# --- The model --------------------------------------------------------------------


async def _ask_model(
    session, identity, history, text, currency, settings: Settings, today, run: _Run
) -> Reply:
    household_id = identity.household.id
    specs = list(chat_tools.TOOLS)
    handlers = dict(chat_tools.handlers_for(session, household_id, currency, today))
    screen_specs, screen_handlers = await chat_service_tools.available(
        identity, session, today
    )
    specs += screen_specs
    handlers |= screen_handlers
    specs.append(chat_sql.RUN_SQL)
    handlers["run_sql"] = chat_sql.handler_for(household_id, currency)
    handlers = {name: _releasing(session, h) for name, h in handlers.items()}

    held = await chat_tools.data_range(session, household_id, currency)
    system = system_prompt(
        currency=currency,
        first=held[0] if held else None,
        last=held[1] if held else None,
        tools=[spec.name for spec in specs],
        today=today,
    )
    messages = [
        Message(m["role"], text=m.get("text") or "")
        for m in history[-HISTORY_MESSAGES:]
        if m.get("role") in ("user", "assistant")
    ] + [Message.user(text)]

    chat_settings = settings.model_copy(
        update={"llm_model": settings.llm_chat_model or settings.llm_model}
    )
    client = build_client(chat_settings)
    run.usage = getattr(client, "usage", None)
    run.budget = settings.llm_chat_token_budget
    # Figures the coach already stated in this conversation were grounded
    # when they were stored; a follow-up ("what was that total again?") may
    # repeat them without asking a tool again.
    earlier = " ".join(
        str(figure)
        for m in history[-HISTORY_MESSAGES:]
        if m.get("role") == "assistant"
        for figure in m.get("figures") or []
    )
    # End the transaction before the model — after the last read above (the
    # features, the data range): on the transaction pooler an open one pins a
    # database connection, and the model may take seconds. Each tool read
    # commits as it finishes (`_releasing`), so none is open at any call.
    await session.commit()
    try:
        return await _grounded(
            client, system, messages, specs, handlers, text, run, earlier
        )
    except LlmError:
        run.outcome = "failed"
        return Reply(UNAVAILABLE, False, "model")
    finally:
        await close_client(client)


async def _grounded(
    client, system, messages, specs, handlers, question, run, earlier=""
) -> Reply:
    # What a figure may also come from: the question, and earlier answers.
    allowed = f"{question} {earlier}"
    result = await _run(client, system, messages, specs, handlers, run)
    named = [(name, json.loads(body)) for name, body in result.tool_results]
    strays = ungrounded(result.text, [body for _, body in named], allowed)
    if strays:
        retry = result.messages + [
            Message.user(
                f"These figures are not in the tool results: {', '.join(strays)}. "
                "Answer again, using only figures exactly as the tools returned them."
            )
        ]
        try:
            result = await _run(client, system, retry, specs, handlers, run)
        except LlmError:
            result = None
        if result is not None:
            named += [(name, json.loads(body)) for name, body in result.tool_results]
        if result is None or ungrounded(
            result.text, [body for _, body in named], allowed
        ):
            run.outcome = "ungrounded"
            return Reply(FALLBACK, False, "model", tools=_record(named))
        run.outcome = "retried"

    text = result.text.strip()
    if (
        any(isinstance(b, dict) and b.get("truncated") for _, b in named)
        and "summary" not in text.lower()
    ):
        text = f"{text} {TRUNCATION_NOTE}"
    if refused(text):
        run.outcome = "refused"
        return Reply(REFUSAL, False, "model", tools=_record(named))
    return Reply(
        text,
        forward_looking(text, run.tools, question),
        "model",
        tools=_record(named),
        figures=[raw for raw, _ in figures_in(text)],
    )


async def _run(client, system, messages, specs, handlers, run: _Run):
    result = await run_tools(
        client,
        system=system,
        messages=messages,
        tools=specs,
        handlers=handlers,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        token_budget=run.budget,
    )
    run.rounds += result.rounds
    run.tools += [name for name, _ in result.tool_results]
    return result


def _releasing(session: AsyncSession, handler):
    """[handler], then commit: a tool's reads end their transaction when the
    tool does, so no connection is held while the model thinks about the
    result."""

    async def handler_then_commit(arguments: dict) -> object:
        try:
            return await handler(arguments)
        finally:
            await session.commit()

    return handler_then_commit


def _record(named: list[tuple[str, object]]) -> list[dict]:
    """What the turn's tools covered, compactly — the tool, its range, how
    many rows it matched and whether it was cut — never the rows."""
    record = []
    for name, body in named:
        entry = {"name": name}
        if isinstance(body, dict):
            entry |= {
                key: body[key]
                for key in ("from", "to", "row_count", "truncated")
                if key in body
            }
        record.append(entry)
    return record


# --- Storing and logging ----------------------------------------------------------


def _store(conversation, history, text, reply: Reply, now: datetime) -> None:
    stamp = now.isoformat()
    turns = [
        {"role": "user", "text": text, "at": stamp},
        {
            "role": "assistant",
            "text": reply.text,
            "at": stamp,
            "answered_by": reply.answered_by,
            "disclaimer": reply.disclaimer,
            "tools": reply.tools,
            "figures": reply.figures,
        },
    ]
    # A new list, so the JSONB column is seen as changed.
    conversation.messages = history + turns
    conversation.title = conversation.title or text[:TITLE_CHARS]
    conversation.last_message_preview = reply.text[:PREVIEW_CHARS]
    # clock_timestamp, not now(): now() is when the transaction began, so two
    # conversations touched in one would tie, and "newest first" would guess.
    conversation.updated_at = func.clock_timestamp()


def _log(household_id, reply: Reply, run: _Run) -> None:
    usage = run.usage
    log.info(
        "chat_turn",
        household_id=str(household_id),
        answered_by=reply.answered_by,
        rounds=run.rounds,
        tools=run.tools,
        input_tokens=getattr(usage, "input_tokens", 0),
        cached_input_tokens=getattr(usage, "cached_input_tokens", 0),
        output_tokens=getattr(usage, "output_tokens", 0),
        cost=getattr(usage, "cost", None),
        cost_estimated=getattr(usage, "cost_estimated", False),
        outcome=run.outcome,
    )
