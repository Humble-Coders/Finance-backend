"""The AI coach's tools over the app's own screens (#71): budget, dashboard,
goals, debts, plan and health score.

Each returns exactly what its endpoint returns — it *calls* the endpoint's
function, with the person's identity and session — so the coach can never
disagree with the screen the person is looking at. Compacted for the model:
ids, version stamps and the heavy series a sentence never needs (the day-by-
day chart, the score's history) are dropped, and #69's `tool_result` caps
what is left.

A tool is offered only when the household has that screen's feature: the
coach must not show figures from a tab the household cannot open.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import budgets as budgets_api
from app.api import dashboard as dashboard_api
from app.api import debts as debts_api
from app.api import goals as goals_api
from app.api import health_score as health_score_api
from app.api import plan as plan_api
from app.services.capabilities import resolve
from app.services.identity import ResolvedIdentity
from app.services.llm import ToolError, ToolSpec

__all__ = ["SERVICE_TOOLS", "available"]

_MONTH = {
    "type": "object",
    "properties": {
        "month": {"type": "string", "description": "YYYY-MM; this month if omitted."}
    },
    "additionalProperties": False,
}
_NOTHING = {"type": "object", "properties": {}, "additionalProperties": False}


@dataclass(frozen=True)
class _Tool:
    spec: ToolSpec
    # The screen's feature; None when the screen has none (debts, dashboard).
    feature: str | None
    call: Callable[[ResolvedIdentity, AsyncSession, dict, date], Awaitable[object]]
    # Keys that never help a sentence.
    drop: frozenset[str] = frozenset()


def _compact(value, drop: frozenset[str]):
    if isinstance(value, dict):
        return {
            key: _compact(item, drop)
            for key, item in value.items()
            if key not in drop
            and key != "id"
            and not key.endswith("_id")
            and not key.endswith("_version")
            and item is not None
        }
    if isinstance(value, list):
        return [_compact(item, drop) for item in value]
    return value


def _month(arguments: dict, today: date) -> str:
    return arguments.get("month") or today.strftime("%Y-%m")


async def _budget(identity, session, arguments, today):
    return await budgets_api.read_budget(
        month=_month(arguments, today), identity=identity, session=session
    )


async def _dashboard(identity, session, arguments, today):
    return await dashboard_api.read_dashboard(
        month=_month(arguments, today), identity=identity, session=session
    )


async def _goals(identity, session, arguments, today):
    return await goals_api.list_goals(identity=identity, session=session)


async def _debts(identity, session, arguments, today):
    return await debts_api.list_debts(identity=identity, session=session)


async def _plan(identity, session, arguments, today):
    return await plan_api.read_plan(
        month=_month(arguments, today), identity=identity, session=session
    )


async def _health_score(identity, session, arguments, today):
    return await health_score_api.read_health_score(identity=identity, session=session)


SERVICE_TOOLS: tuple[_Tool, ...] = (
    _Tool(
        ToolSpec(
            "budget",
            "A month's budget as the Budget tab shows it: each line's allocation "
            "and spending, left_to_spend, commitments and what is still to come.",
            _MONTH,
        ),
        budgets_api.FEATURE,
        _budget,
        frozenset({"ai_kind", "ai_extra", "ai_savings", "ai_advice", "ai_review_due"})
        | frozenset({"learning", "status", "spending_by_category"}),
    ),
    _Tool(
        ToolSpec(
            "dashboard",
            "A month as Home shows it: income, expenses, net, spending by "
            "category, investments, debts and commitments.",
            _MONTH,
        ),
        None,
        _dashboard,
        frozenset({"daily", "as_of", "learning", "budget", "health_score"}),
    ),
    _Tool(
        ToolSpec(
            "goals",
            "The household's savings goals, what each needs a month, and whether "
            "it is on track.",
            _NOTHING,
        ),
        goals_api.FEATURE,
        _goals,
    ),
    _Tool(
        ToolSpec(
            "debts", "The household's debts, balances, rates and minimums.", _NOTHING
        ),
        None,
        _debts,
    ),
    _Tool(
        ToolSpec(
            "plan",
            "The month's plan for money left over: what goes to debts, goals and "
            "savings, and any shortfall.",
            _MONTH,
        ),
        plan_api.FEATURE,
        _plan,
        frozenset({"preferences"}),
    ),
    _Tool(
        ToolSpec(
            "health_score",
            "The Money Health Score and what each part of it is made of.",
            _NOTHING,
        ),
        health_score_api.FEATURE,
        _health_score,
        frozenset({"history", "notice", "held_from", "learning"}),
    ),
)


async def available(
    identity: ResolvedIdentity, session: AsyncSession, today: date
) -> tuple[list[ToolSpec], dict[str, Callable[[dict], Awaitable[object]]]]:
    """The screen tools this household may use, and their handlers."""
    features = (await resolve(session, identity.household)).features
    specs, handlers = [], {}
    for tool in SERVICE_TOOLS:
        if tool.feature is not None:
            feature = features.get(tool.feature)
            if feature is None or not feature.enabled:
                continue
        specs.append(tool.spec)
        handlers[tool.spec.name] = _bind(tool, identity, session, today)
    return specs, handlers


def _bind(tool: _Tool, identity, session, today):
    async def handler(arguments: dict) -> object:
        try:
            out = await tool.call(identity, session, arguments, today)
        except HTTPException as refused:
            # A bad month, or a state the screen itself answers with an error:
            # back to the model to correct, not a crash.
            detail = refused.detail
            code = detail.get("code") if isinstance(detail, dict) else detail
            raise ToolError(f"{tool.spec.name} refused: {code}") from None
        return _compact(out.model_dump(mode="json"), tool.drop)

    return handler
