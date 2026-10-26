"""Your plan's two diagnostics (product decision, 2026-10-26).

* **allocation** — "Pay debt, fund goals, or keep it in savings?" The person
  says how much they want to keep in savings and how much they have; the
  plan's own split (`plan.split`, the arithmetic Your plan already uses) runs
  on those figures, and the AI explains the order in words.
* **invest** — "Should I invest, and how much?" The server checks what has to
  come first — the savings buffer full, no debt above the rate line, the goals
  covered — and works out what is free to invest each month; the AI says
  whether and why, and which *kinds* of account are usual where the person
  lives. Never a product, a fund or a company.

**Every figure is the server's** (`CLAUDE.md` → Financial math). The AI is
given the figures and answers in words; a sentence with a digit in it is
dropped (`ai_budget.figure_free`). What the AI is sent carries no names.

**Cost.** A run is stored with a fingerprint of the figures it was given. The
same figures again return that run; a month allows `MAX_RUNS_A_MONTH` fresh
runs of each kind.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import exponent_for, from_minor_units
from app.models.derived import PlanDiagnostic
from app.models.identity import Household
from app.models.setup import Investment
from app.services import plan, plan_view
from app.services.ai_budget import _answer, figure_free
from app.services.llm import LlmClient

log = structlog.get_logger()

PROMPT_VERSION = "2026-10-26.1"

ALLOCATION = "allocation"
INVEST = "invest"
KINDS = (ALLOCATION, INVEST)
MAX_RUNS_A_MONTH = 4

# What investing is assumed to return a year, for the comparison with paying
# a debt down: shown beside the figure as an assumption, never a promise.
ASSUMED_RETURN_BPS = 500

MAX_OUTPUT_TOKENS = 1_200
TEXT_MAX_CHARS = 500
STEP_KEYS = ("savings", "goals", "debt", "invest")


class LimitReached(Exception):
    """This month's fresh runs of the diagnostic are used up."""


def first_year_gain(per_month: int, rate_bps: int) -> int:
    """What [per_month] put in each month for a year earns, or saves in
    interest, at [rate_bps] a year: month m's money works for 12 - m + 1
    months, so the year totals per_month x rate/12 x 78."""
    return int(Decimal(per_month) * rate_bps / 10_000 / 12 * 78)


@dataclass
class Facts:
    """The figures a run is about: `figures` for the app (names included),
    `sent` for the AI (no names)."""

    figures: dict
    sent: dict

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.sent, sort_keys=True).encode()
        ).hexdigest()


def _rate_percent(bps: int | None) -> str | None:
    return None if bps is None else str((Decimal(bps) / 100).quantize(Decimal("0.01")))


async def facts(
    session: AsyncSession,
    household: Household,
    currency: str,
    today: date,
    kind: str,
    savings_target: int,
    savings_now: int,
) -> Facts:
    """The server's figures for [kind], from the plan run on the person's own
    savings amounts."""
    view = await plan_view.build(
        session,
        household.id,
        currency,
        today.replace(day=1),
        today,
        buffer=(savings_target, savings_now),
    )
    split = view.split
    line = view.preferences.rate_line_bps
    month = view.month
    terms = {d.id: plan_view.terms_of(d) for d in view.debts}
    expensive = [
        d
        for d in view.debts
        if d.balance_minor_units > 0 and plan._above(terms[d.id], month, line)
    ]
    focus = next((d for d in view.debts if d.id == view.focus), None)
    focus_rate = plan.rate_on(terms[focus.id], month) if focus else None
    if focus_rate is None and expensive:
        focus_rate = max(plan.rate_on(terms[d.id], month) or 0 for d in expensive)

    goals = [
        {"id": str(goal_id), "name": view.goal_names.get(goal_id, ""), "amount": amount}
        for goal_id, amount in split.goals
    ]
    common = {
        "leftover": view.leftover,
        "savings_target": savings_target,
        "savings_now": savings_now,
        "savings_gap": max(savings_target - savings_now, 0),
        "to_savings": split.buffer,
        "to_goals": sum(g["amount"] for g in goals),
        "to_debt": split.extra_debt,
        "to_invest": split.save_or_invest,
        "rate_line_percent": _rate_percent(line),
    }

    if kind == ALLOCATION:
        saved = (
            view.minimums_only.total_interest - view.schedule.total_interest
            if view.minimums_only.total_interest is not None
            and view.schedule.total_interest is not None
            else None
        )
        per_100 = 100 * 10 ** exponent_for(currency)
        figures = common | {
            "goals": goals,
            "debt_first": focus.name if focus else None,
            "debt_first_rate_percent": _rate_percent(focus_rate),
            "interest_saved": saved,
            "debt_free": view.schedule.debt_free.isoformat()
            if view.schedule.debt_free
            else None,
            "debt_free_minimums": (
                view.minimums_only.debt_free.isoformat()
                if view.minimums_only.debt_free
                else None
            ),
            "per_100": per_100,
            "debt_per_100": first_year_gain(per_100, focus_rate)
            if focus_rate
            else None,
            "invest_per_100": first_year_gain(per_100, ASSUMED_RETURN_BPS),
            "assumed_return_percent": _rate_percent(ASSUMED_RETURN_BPS),
        }
        sent = {
            k: v for k, v in figures.items() if k not in {"goals", "debt_first"}
        } | {
            "goals_by_priority": [{"amount": g["amount"]} for g in goals],
            "debts": [
                {
                    "kind": d.kind.value,
                    "rate_percent": _rate_percent(plan.rate_on(terms[d.id], month)),
                    "balance": d.balance_minor_units,
                }
                for d in view.debts
            ],
        }
        return Facts(_money(figures, currency), _money(sent, currency))

    invested = await session.scalar(
        select(func.coalesce(func.sum(Investment.amount_minor_units), 0)).where(
            Investment.household_id == household.id, Investment.currency == currency
        )
    )
    given = dict(split.goals)
    covered = all(
        given.get(ask.id, 0) >= min(ask.need, ask.remaining) for ask in view.asks
    )
    figures = common | {
        "buffer_full": savings_target <= savings_now,
        "expensive_debts": [d.name for d in expensive],
        "goals_covered": covered,
        "existing_investments": int(invested or 0),
        "assumed_return_percent": _rate_percent(ASSUMED_RETURN_BPS),
        "country": household.country_code,
    }
    sent = {k: v for k, v in figures.items() if k != "expensive_debts"} | {
        "expensive_debt_count": len(expensive),
    }
    return Facts(_money(figures, currency), _money(sent, currency))


_MONEY_KEYS = {
    "leftover",
    "savings_target",
    "savings_now",
    "savings_gap",
    "to_savings",
    "to_goals",
    "to_debt",
    "to_invest",
    "interest_saved",
    "per_100",
    "debt_per_100",
    "invest_per_100",
    "existing_investments",
    "amount",
    "balance",
}


def _money(value, currency: str):
    """Minor units under money keys written as decimal strings, all the way down."""
    if isinstance(value, dict):
        return {
            k: (
                from_minor_units(v, currency)
                if k in _MONEY_KEYS and isinstance(v, int) and not isinstance(v, bool)
                else _money(v, currency)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_money(v, currency) for v in value]
    return value


# --- Asking ------------------------------------------------------------------

_SHARED = """You help a person read their own plan in a personal finance app.
The app has already worked out every figure; you explain them in plain words.
Rules: no digits at all in your answer — no amounts, percentages or dates;
guidance, not financial advice; never name a product, a fund, a bank or a
company. Answer with JSON only."""

_ALLOCATION = (
    _SHARED
    + """
The question is where this month's money left over should go: the person's
savings (up to the amount they want to keep), their goals by priority, extra
payments on their debts, or investing. The app's plan fills savings first,
then splits between goals and the highest-rate debt while any debt charges
more than the rate line, then invests what is left. Explain why this order
fits their figures — compare what paying the debt saves with what investing
might earn ("debt_per_100" against "invest_per_100") — and say plainly if
their savings amount looks low or high for their spending.
{"headline": "<one sentence>",
 "steps": [{"to": "savings|goals|debt|invest", "why": "<one sentence>"}],
 "note": "<one or two sentences>"}"""
)

_INVEST = (
    _SHARED
    + """
The question is whether the person should invest now, and how much. Investing
comes after: the savings buffer full, no debt above the rate line, and the
goals covered. Say whether they are ready, what is missing if not, whether the
amount the app found free to invest ("to_invest") is a sensible start, and —
for the country given — which kinds of account people there commonly use for
long-term investing (for example a tax-advantaged savings or retirement
account), in general terms only.
{"headline": "<one sentence>", "why": "<two sentences>",
 "where": "<one or two sentences, kinds of account only>",
 "next": "<one sentence>"}"""
)


def _advice(kind: str, data: dict) -> dict:
    if kind == ALLOCATION:
        steps = []
        for item in data.get("steps") or []:
            if isinstance(item, dict) and item.get("to") in STEP_KEYS:
                why = figure_free(item.get("why"), TEXT_MAX_CHARS)
                if why:
                    steps.append({"to": item["to"], "why": why})
        return {
            "headline": figure_free(data.get("headline"), TEXT_MAX_CHARS),
            "steps": steps,
            "note": figure_free(data.get("note"), TEXT_MAX_CHARS),
        }
    return {
        key: figure_free(data.get(key), TEXT_MAX_CHARS)
        for key in ("headline", "why", "where", "next")
    }


async def runs_this_month(
    session: AsyncSession, household_id: uuid.UUID, kind: str, month: date
) -> int:
    return int(
        await session.scalar(
            select(func.count()).where(
                PlanDiagnostic.household_id == household_id,
                PlanDiagnostic.kind == kind,
                PlanDiagnostic.month == month,
            )
        )
        or 0
    )


async def latest(
    session: AsyncSession, household_id: uuid.UUID, kind: str
) -> PlanDiagnostic | None:
    return await session.scalar(
        select(PlanDiagnostic)
        .where(PlanDiagnostic.household_id == household_id, PlanDiagnostic.kind == kind)
        .order_by(PlanDiagnostic.created_at.desc(), PlanDiagnostic.id.desc())
        .limit(1)
    )


async def remembered(session: AsyncSession, household_id: uuid.UUID) -> dict | None:
    """The savings amounts the person last ran a diagnostic with."""
    rows = await session.scalars(
        select(PlanDiagnostic)
        .where(PlanDiagnostic.household_id == household_id)
        .order_by(PlanDiagnostic.created_at.desc())
        .limit(1)
    )
    row = rows.first()
    return row.inputs if row else None


async def run(
    session: AsyncSession,
    client_factory,
    household: Household,
    currency: str,
    today: date,
    kind: str,
    savings_target: int,
    savings_now: int,
) -> tuple[PlanDiagnostic, bool]:
    """(the run, whether it is a stored one). A fresh run only when the
    figures changed since the last; `LimitReached` past this month's runs."""
    month = today.replace(day=1)
    found = await facts(
        session, household, currency, today, kind, savings_target, savings_now
    )
    last = await latest(session, household.id, kind)
    if last is not None and last.fingerprint == found.fingerprint:
        return last, True
    if await runs_this_month(session, household.id, kind, month) >= MAX_RUNS_A_MONTH:
        raise LimitReached()

    client: LlmClient = client_factory()
    try:
        raw = await client.complete(
            system=_ALLOCATION if kind == ALLOCATION else _INVEST,
            user=json.dumps(found.sent, indent=1),
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
    finally:
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()
    advice = _advice(kind, _answer(raw))
    row = PlanDiagnostic(
        household_id=household.id,
        kind=kind,
        month=month,
        currency=currency,
        fingerprint=found.fingerprint,
        inputs={"savings_target": savings_target, "savings_now": savings_now},
        figures=found.figures,
        advice=advice,
        model=client.model,
        # Stamped here rather than by the database's now(), which is the
        # transaction's start: two runs in one transaction would tie, and the
        # latest run is found by this.
        created_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    log.info(
        "plan_diagnostic_run",
        kind=kind,
        prompt_version=PROMPT_VERSION,
        model=client.model,
    )
    return row, False
