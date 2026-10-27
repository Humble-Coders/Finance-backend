"""The budget screen's spending check (product decision, 2026-10-27).

How a month's spending sits against the person's income under the app's one
rule — 50/30/20, adjusted (`ai_budget`): needs, wants and what was saved, each
as a share of income beside what the rule aims for, and the AI's advice on
spending the right way for that income — a few tips for the month, and one
for each category it thinks most worth changing.

**Every figure is the server's** (`CLAUDE.md` → Financial math). The AI is
given every figure the app holds that bears on spending — income, this
month's spending by category with each one's usual amount and line, one-offs,
commitments, debts, goals and savings — and answers in words; a sentence with
a digit in it is dropped (`ai_budget.figure_free`). It is sent no names.

**Cost.** A run is stored as a `plan_diagnostic` of kind `spending`, with a
fingerprint of what the AI was sent. The same figures again return that run;
each month checked allows `MAX_RUNS_A_MONTH` fresh runs. An ended month's
figures rarely change, so checking one again is almost always the stored run.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import from_minor_units
from app.models.derived import PlanDiagnostic
from app.models.setup import Investment
from app.services import ai_budget
from app.services.ai_budget import _answer, figure_free
from app.services.budget import budget_for
from app.services.dashboard import month_bounds
from app.services.llm import LlmClient

log = structlog.get_logger()

PROMPT_VERSION = "2026-10-27.1"
KIND = "spending"
MAX_RUNS_A_MONTH = 4

MAX_OUTPUT_TOKENS = 1_000
TEXT_MAX_CHARS = 300
MAX_TIPS = 3
MAX_CATEGORY_TIPS = 4


class LimitReached(Exception):
    """This month's fresh runs are used up; the last answer stays."""


def share_percent(part: int, whole: int) -> int:
    """[part] as a whole percentage of [whole], rounded half up; 0 without a whole."""
    if whole <= 0:
        return 0
    return (max(part, 0) * 200 + whole) // (2 * whole)


@dataclass(frozen=True)
class Figures:
    """One month's spending against income, in minor units of `currency`."""

    month: date
    currency: str
    month_ended: bool
    income: int
    # Needs include what went to debt; wants are the rule's wants.
    needs: int
    wants: int
    saved: int
    # Spending with no category yet: in neither share.
    unsorted: int
    needs_percent: int
    wants_percent: int
    saved_percent: int
    needs_aim_percent: int
    wants_aim_percent: int
    savings_aim_percent: int
    # Category id by slug, for each category spent in this month: what a tip
    # is attached to.
    category_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    # What the AI is sent, and its fingerprint.
    sent: dict = field(default_factory=dict)
    fingerprint: str = ""

    @property
    def needs_over(self) -> bool:
        return self.needs_percent > self.needs_aim_percent

    @property
    def wants_over(self) -> bool:
        return self.wants_percent > self.wants_aim_percent

    @property
    def savings_short(self) -> bool:
        return self.saved_percent < self.savings_aim_percent


async def figures(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    month: date,
    today: date,
    *,
    ready: bool,
) -> Figures:
    """[month]'s check. `ai_budget.NoIncome` without an income from setup."""
    first, last = month_bounds(month)
    inputs = await ai_budget._inputs(session, household_id, currency, min(today, last))
    view = await budget_for(session, household_id, currency, first, today, ready=ready)
    income = inputs.income

    spent: dict[str, dict] = {}
    category_ids: dict[str, uuid.UUID] = {}
    for item in [*view.lines, *view.unbudgeted]:
        if item.spent <= 0 and getattr(item, "allocated", 0) <= 0:
            continue
        category_ids.setdefault(item.slug, item.category_id)
        row = spent.setdefault(
            item.slug,
            {"category": item.slug, "kind": ai_budget.kind_of(item.slug), "spent": 0},
        )
        row["spent"] += item.spent
        row["line"] = row.get("line", 0) + getattr(item, "allocated", 0)
        if item.one_off:
            row["one_off"] = row.get("one_off", 0) + item.one_off
    debt_paid = view.debt.spent if view.debt is not None else 0
    needs = sum(r["spent"] for r in spent.values() if r["kind"] == "need") + debt_paid
    wants = sum(r["spent"] for r in spent.values() if r["kind"] == "want")
    saved = max(view.month_saved, 0)

    # The rule's savings, raised to what the goals need each month.
    savings_aim = min(
        share_percent(
            max(income * ai_budget.SAVINGS_PERCENT // 100, inputs.goals_need), income
        ),
        100,
    )

    invested = int(
        await session.scalar(
            select(func.coalesce(func.sum(Investment.amount_minor_units), 0)).where(
                Investment.household_id == household_id,
                Investment.currency == currency,
            )
        )
        or 0
    )

    def money(minor: int) -> str:
        return from_minor_units(minor, currency)

    sent = {
        "currency": currency,
        "month_ended": view.month_ended,
        "days_left_in_month": 0 if view.month_ended else (last - today).days + 1,
        "monthly_income": money(income),
        "income_received_this_month": money(view.month_income),
        "rule": {
            "needs_percent": ai_budget.NEEDS_PERCENT,
            "wants_percent": ai_budget.WANTS_PERCENT,
            "savings_percent": savings_aim,
        },
        "this_month": {
            "needs": money(needs),
            "wants": money(wants),
            "to_debt": money(debt_paid),
            "saved": money(saved),
            "not_sorted_yet": money(view.uncategorised_spent),
        },
        "by_category": [
            {
                "category": r["category"],
                "kind": r["kind"],
                "spent": money(r["spent"]),
                "budget_line": money(r.get("line", 0)),
                "usual_monthly": money(inputs.history.get(r["category"], 0)),
                **({"one_off": money(r["one_off"])} if r.get("one_off") else {}),
            }
            for r in sorted(spent.values(), key=lambda r: -r["spent"])
        ],
        "commitments_by_category": {s: money(a) for s, a in inputs.floors.items()},
        "savings_target": money(view.savings.allocated if view.savings else 0),
        "debts": inputs.debts,
        "goals": inputs.goals,
        "goals_monthly_need": money(inputs.goals_need),
        "invested_so_far": money(invested),
    }
    fingerprint = hashlib.sha256(
        json.dumps({"v": PROMPT_VERSION, **sent}, sort_keys=True).encode()
    ).hexdigest()
    return Figures(
        month=first,
        currency=currency,
        month_ended=view.month_ended,
        income=income,
        needs=needs,
        wants=wants,
        saved=saved,
        unsorted=view.uncategorised_spent,
        needs_percent=share_percent(needs, income),
        wants_percent=share_percent(wants, income),
        saved_percent=share_percent(saved, income),
        needs_aim_percent=ai_budget.NEEDS_PERCENT,
        wants_aim_percent=ai_budget.WANTS_PERCENT,
        savings_aim_percent=savings_aim,
        category_ids=category_ids,
        sent=sent,
        fingerprint=fingerprint,
    )


# --- Asking ------------------------------------------------------------------

_SYSTEM = """You help a person spend the right way for their income in a
personal finance app. The app follows the 50/30/20 rule, adjusted: about half
of income for needs (commitments, groceries, utilities, transport, debt
payments), about a third for wants, and the rest saved — more when their goals
need it ("rule"). You are given every figure the app holds about this month;
the app has already worked them out, so do not calculate new ones.

Write:
- "headline": one sentence on how this month's spending sits against their
  income.
- "tips": up to three short sentences, the most useful first — what to cut or
  keep, and where money left over is best put (the emergency buffer, then goals
  by priority, then the highest-rate debt).
- "categories": up to four categories from "by_category" most worth changing
  for their income, each with one short practical tip.
A running month is not finished ("days_left_in_month"): judge it by pace, not
as if it had ended. Spending marked "one_off" will not happen again — do not
tell them to cut a category because of it.
Rules: no digits at all — no amounts, percentages or dates; plain words;
guidance, not financial advice; never name a product, a bank or a company.
Answer with JSON only:
{"headline": "<text>", "tips": ["<text>"],
 "categories": [{"category": "<slug>", "tip": "<text>"}]}"""


def _advice(data: dict, known: set[str]) -> dict:
    tips = [
        tip
        for raw in (data.get("tips") or [])[:MAX_TIPS]
        if (tip := figure_free(raw, TEXT_MAX_CHARS))
    ]
    categories = []
    for item in data.get("categories") or []:
        if len(categories) >= MAX_CATEGORY_TIPS:
            break
        if not isinstance(item, dict) or item.get("category") not in known:
            continue
        if any(c["category"] == item["category"] for c in categories):
            continue
        tip = figure_free(item.get("tip"), TEXT_MAX_CHARS)
        if tip:
            categories.append({"category": item["category"], "tip": tip})
    return {
        "headline": figure_free(data.get("headline"), TEXT_MAX_CHARS),
        "tips": tips,
        "categories": categories,
    }


async def runs_for(session: AsyncSession, household_id: uuid.UUID, month: date) -> int:
    """Fresh runs made for [month]."""
    return int(
        await session.scalar(
            select(func.count()).where(
                PlanDiagnostic.household_id == household_id,
                PlanDiagnostic.kind == KIND,
                PlanDiagnostic.month == month_bounds(month)[0],
            )
        )
        or 0
    )


async def latest(
    session: AsyncSession, household_id: uuid.UUID, month: date
) -> PlanDiagnostic | None:
    """The last check of [month]."""
    return await session.scalar(
        select(PlanDiagnostic)
        .where(
            PlanDiagnostic.household_id == household_id,
            PlanDiagnostic.kind == KIND,
            PlanDiagnostic.month == month_bounds(month)[0],
        )
        .order_by(PlanDiagnostic.created_at.desc(), PlanDiagnostic.id.desc())
        .limit(1)
    )


async def run(
    session: AsyncSession,
    client_factory,
    found: Figures,
    household_id: uuid.UUID,
) -> tuple[PlanDiagnostic, bool]:
    """(the run, whether it is a stored one). A fresh run only when the
    figures changed since the month's last; `LimitReached` past its runs."""
    last = await latest(session, household_id, found.month)
    if last is not None and last.fingerprint == found.fingerprint:
        return last, True
    if await runs_for(session, household_id, found.month) >= MAX_RUNS_A_MONTH:
        raise LimitReached()

    client: LlmClient = client_factory()
    try:
        raw = await client.complete(
            system=_SYSTEM,
            user=json.dumps(found.sent, indent=1),
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
    finally:
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()
    advice = _advice(_answer(raw), set(found.category_ids))
    row = PlanDiagnostic(
        household_id=household_id,
        kind=KIND,
        month=found.month,
        currency=found.currency,
        fingerprint=found.fingerprint,
        inputs={},
        figures={},
        advice=advice,
        model=client.model,
        # Stamped here, not by the database's now(): the latest run is found by
        # this, and two in one transaction would tie.
        created_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    log.info(
        "spending_check_run",
        prompt_version=PROMPT_VERSION,
        model=client.model,
        tips=len(advice["tips"]),
        categories=len(advice["categories"]),
    )
    return row, False
