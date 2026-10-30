"""Your plan's "Where it goes", as the AI proposes it (redesign, 2026-10-31).

The person orders the four categories and gives each a percentage
(`plan.Preferences.weights`); the AI proposes the amounts from those choices
and the household's figures, and `plan.checked` holds the proposal to the same
limits the rules keep — none negative, none over what its category can use,
the whole exactly the money left over. **Every figure shown is the server's**
(`CLAUDE.md` → Financial math); the AI's words carry none
(`ai_budget.figure_free`). What the AI is sent carries no names.

**Cost.** A month's split is kept with a fingerprint of what it was worked out
from. The same again returns it — the person's own edits included — without
asking; a change asks afresh, at most `MAX_AI_RUNS` times a month. Past that,
or when the AI may not be asked (no consent, the provider not confirmed) or
fails, the rules split it (`plan.split`), and the screen says so.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import date

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import from_minor_units, to_minor_units
from app.models.derived import PlanSplit
from app.services import plan
from app.services.ai_budget import _answer, _bare_amount, figure_free
from app.services.llm import LlmClient, LlmError
from app.services.plan_view import Decided

log = structlog.get_logger()

PROMPT_VERSION = "2026-10-31.1"
MAX_AI_RUNS = 12
MAX_OUTPUT_TOKENS = 700
WHY_MAX_CHARS = 400

AI = "ai"
RULES = "rules"

_SYSTEM = """You divide one month's money left over in a personal finance app.
The person has put four places in their order of priority and given each a
percentage of the money: their emergency fund, extra payments on their debts,
their other goals, and saving or investing. Start from their percentages, in
their order. Move money between places only for a reason in the figures: a
place that cannot use its share (what it "can_use" is less), a goal whose date
is close and needs more to stay on pace ("monthly_need"), or a debt whose rate
is far above what saving earns. Never give a place more than it "can_use";
saving or investing has no limit. The amounts must add up to "money_left_over"
exactly.
Answer with JSON only:
{"emergency_fund": "<amount>", "debt": "<amount>",
 "goals": [{"index": <the goal's index>, "amount": "<amount>"}],
 "save_or_invest": "<amount>",
 "why": "<one or two sentences, no digits at all>"}
Amounts are plain numbers with two decimals. Guidance, not financial advice;
never name a product, a fund, a bank or a company."""


# Asks the model, or raises `LlmError`. Built only when a fresh run is needed.
ClientFactory = Callable[[], LlmClient]
# Whether the AI may be asked for this household now: consent given, the
# provider confirmed. Checked only when a fresh run is needed.
MayAsk = Callable[[], Awaitable[bool]]


def _sent(
    leftover: int,
    preferences: plan.Preferences,
    limits: plan.Limits,
    rates: list[dict],
    currency: str,
) -> dict:
    """What the AI is told — figures only, no names."""

    def money(minor: int | None) -> str | None:
        return None if minor is None else from_minor_units(minor, currency)

    return {
        "money_left_over": money(leftover),
        "places": [
            {
                "place": w.category,
                "priority": i + 1,
                "percent": str(w.bps / 100),
                "can_use": money(limits.of(w.category)),
            }
            for i, w in enumerate(preferences.weights)
        ],
        "goals_by_priority": [
            {
                "index": i,
                "monthly_need": money(g.need),
                "still_needed": money(g.remaining),
            }
            for i, g in enumerate(limits.goals)
        ],
        "debts": rates,
    }


def fingerprint(sent: dict) -> str:
    return hashlib.sha256(json.dumps(sent, sort_keys=True).encode()).hexdigest()


def _figures(split: plan.Split) -> dict:
    return {
        "emergency": split.emergency,
        "goals": {str(gid): amount for gid, amount in split.goals},
        "debt": split.extra_debt,
        "save_or_invest": split.save_or_invest,
    }


def _split_of(figures: dict, limits: plan.Limits) -> plan.Split:
    goals = figures.get("goals") or {}
    return plan.Split(
        buffer=0,
        emergency=int(figures.get("emergency", 0)),
        goals=tuple(
            (g.id, int(goals[str(g.id)])) for g in limits.goals if goals.get(str(g.id))
        ),
        extra_debt=int(figures.get("debt", 0)),
        save_or_invest=int(figures.get("save_or_invest", 0)),
    )


def _amount(raw: object, currency: str) -> int:
    try:
        return max(to_minor_units(_bare_amount(str(raw)), currency), 0)
    except Exception:  # noqa: BLE001 — an unreadable amount is simply none
        return 0


def _proposal(
    answer: dict, limits: plan.Limits, currency: str
) -> tuple[dict[str, int], dict[uuid.UUID, int]]:
    proposed = {
        plan.EMERGENCY: _amount(answer.get("emergency_fund", 0), currency),
        plan.DEBT: _amount(answer.get("debt", 0), currency),
        plan.SAVE_OR_INVEST: _amount(answer.get("save_or_invest", 0), currency),
    }
    goals: dict[uuid.UUID, int] = {}
    for item in answer.get("goals") or []:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if isinstance(index, int) and 0 <= index < len(limits.goals):
            goals[limits.goals[index].id] = _amount(item.get("amount", 0), currency)
    proposed[plan.GOALS] = sum(goals.values())
    return proposed, goals


async def stored(
    session: AsyncSession, household_id: uuid.UUID, month: date
) -> PlanSplit | None:
    return await session.scalar(
        select(PlanSplit).where(
            PlanSplit.household_id == household_id, PlanSplit.month == month
        )
    )


def decider(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    month: date,
    rates: list[dict],
    client_factory: ClientFactory,
    may_ask: MayAsk,
):
    """A `plan_view.Decide` that keeps the month's split: the kept one while
    nothing it depends on changed, else the AI's proposal checked, else the
    rules."""

    async def keep(
        row: PlanSplit | None,
        mark: str,
        split: plan.Split,
        source: str,
        why: str | None = None,
        model: str | None = None,
        ran: bool = False,
    ) -> Decided:
        if row is None:
            row = PlanSplit(household_id=household_id, month=month, ai_runs=0)
            session.add(row)
        row.currency = currency
        row.fingerprint = mark
        row.figures = _figures(split)
        row.source = source
        row.why = why
        row.model = model
        row.edited = False
        if ran:
            row.ai_runs = (row.ai_runs or 0) + 1
        await session.flush()
        return Decided(split, source, why, False)

    async def decide(
        leftover: int, preferences: plan.Preferences, limits: plan.Limits
    ) -> Decided:
        sent = _sent(leftover, preferences, limits, rates, currency)
        mark = fingerprint(sent)
        row = await stored(session, household_id, month)
        if row is not None and row.fingerprint == mark and row.currency == currency:
            return Decided(
                _split_of(row.figures, limits), row.source, row.why, row.edited
            )
        by_rules = plan.split(leftover, preferences, limits)
        runs = row.ai_runs if row is not None else 0
        if leftover <= 0 or runs >= MAX_AI_RUNS or not await may_ask():
            return await keep(row, mark, by_rules, RULES)

        client = client_factory()
        try:
            raw = await client.complete(
                system=_SYSTEM,
                user=json.dumps(sent, indent=1),
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
            answer = _answer(raw)
        except LlmError as error:
            # Not kept as this fingerprint's answer: the next read asks again.
            log.warning("plan_split_ai_failed", error=str(error))
            return Decided(by_rules, RULES)
        finally:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()
        proposed, goals = _proposal(answer, limits, currency)
        split = plan.checked(leftover, preferences, limits, proposed, goals)
        why = figure_free(answer.get("why"), WHY_MAX_CHARS)
        log.info("plan_split_run", prompt_version=PROMPT_VERSION, model=client.model)
        return await keep(row, mark, split, AI, why, client.model, ran=True)

    return decide


async def remember_edit(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    month: date,
    rates: list[dict],
    leftover: int,
    preferences: plan.Preferences,
    limits: plan.Limits,
    split: plan.Split,
) -> Decided:
    """Keep the person's own amounts as the month's split, marked with what
    the figures are now — applying an edit can change them (a goal's planned
    amount is what it asks without a date) — so the next read returns the
    edit instead of working the split out afresh."""
    row = await stored(session, household_id, month)
    if row is None:
        row = PlanSplit(household_id=household_id, month=month, ai_runs=0, source=RULES)
        session.add(row)
    row.currency = currency
    row.fingerprint = fingerprint(_sent(leftover, preferences, limits, rates, currency))
    row.figures = _figures(split)
    row.edited = True
    await session.flush()
    return Decided(split, row.source, row.why, True)
