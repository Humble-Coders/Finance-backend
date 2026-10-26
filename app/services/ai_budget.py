"""The AI's budget suggestions: the first budget at setup, and a monthly review.

**The server owns every figure.** The AI never sets a number anyone sees
without the server checking it (`CLAUDE.md` → Financial math). The server
works out what is fixed — the commitments, the debts' minimum payments, the
savings target the rule asks for — and the AI only divides what is left
across the spending categories, with a short reason for each. `checked` then
rounds, floors each line at its commitments, keeps wants within their share,
and scales down anything that does not fit, so the lines, debt and savings
always add up to the expected income. Reasons and advice are kept only where
they contain no figures.

**The rule is 50/30/20, adjusted** (product decision, 2026-10-25): about half
of income for needs — commitments, debt minimums, groceries, utilities,
transport — 30% for wants, and 20% for savings, raised to what the goals need
each month. When fixed costs leave less than 20%, savings takes what is left
and the AI is told to keep wants lean.

**What is sent** is figures only: amounts by category slug, debt kinds, rates
and minimums, goal kinds and monthly needs. No names — not a goal's, a
debt's, a commitment's or a merchant's (PRD A.3, data minimization).

**Cost** is bounded by storage: a setup suggestion is made at most
`MAX_SETUP_ATTEMPTS` times, and a review once per household per month —
asking again returns what was stored.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import exponent_for, from_minor_units, round_up_to_whole_unit
from app.models.categorization import Category
from app.models.derived import AiBudgetSuggestion
from app.models.planning import Debt, Goal
from app.models.setup import FinancialProfile
from app.services import commitments
from app.services.dashboard import figures_by_month, months_back
from app.services.goals import monthly_need, project
from app.services.llm import LlmClient, LlmError

log = structlog.get_logger()

PROMPT_VERSION = "2026-10-25.1"

SETUP = "setup"
REVIEW = "review"
MAX_SETUP_ATTEMPTS = 3

# Needs and wants, by the shared categories' slugs. Debt payments and savings
# have rules of their own; income and transfers are not spending.
NEED_SLUGS = (
    "rent",
    "utilities",
    "groceries",
    "transport",
    "insurance",
    "healthcare",
    "education",
    "fees",
)
WANT_SLUGS = (
    "dining",
    "entertainment",
    "shopping",
    "subscriptions",
    "travel",
    "personal_care",
    "gifts_donations",
    "other",
)
SPENDING_SLUGS = NEED_SLUGS + WANT_SLUGS

NEEDS_PERCENT = 50
WANTS_PERCENT = 30
SAVINGS_PERCENT = 20

# Enough for every category with a reason, and the note.
MAX_OUTPUT_TOKENS = 1_500
REASON_MAX_CHARS = 160
ADVICE_MAX_CHARS = 600


class NoIncome(Exception):
    """Setup has no monthly income: there is nothing to divide."""


class TooManyAttempts(Exception):
    """The setup suggestion has been asked for `MAX_SETUP_ATTEMPTS` times."""


# --- The arithmetic ----------------------------------------------------------


@dataclass(frozen=True)
class Envelope:
    """What is fixed before the AI divides anything, in minor units."""

    income: int
    # Each category's commitments: the least its line holds.
    floors: dict[str, int]
    debt: int
    savings: int
    # Income less floors, debt and savings: the AI's to divide.
    flexible: int
    needs_cap: int
    wants_cap: int
    # True when fixed costs leave less than the rule's savings.
    savings_short: bool


def envelope(
    income: int,
    floors: Mapping[str, int],
    debt_minimums: int,
    goals_need: int,
    currency: str,
) -> Envelope:
    """The fixed parts of a budget under 50/30/20, adjusted."""
    held = {
        slug: round_up_to_whole_unit(amount, currency)
        for slug, amount in floors.items()
        if slug in SPENDING_SLUGS and amount > 0
    }
    debt = max(debt_minimums, 0)
    rule = round_up_to_whole_unit(Decimal(income) * SAVINGS_PERCENT / 100, currency)
    wanted = max(rule, goals_need)
    room = income - sum(held.values()) - debt
    savings = max(min(wanted, room), 0)
    return Envelope(
        income=income,
        floors=held,
        debt=debt,
        savings=savings,
        flexible=max(room - savings, 0),
        needs_cap=income * NEEDS_PERCENT // 100,
        wants_cap=income * WANTS_PERCENT // 100,
        savings_short=savings < wanted,
    )


@dataclass(frozen=True)
class Line:
    slug: str
    amount: int
    reason: str | None


@dataclass(frozen=True)
class Checked:
    lines: list[Line]
    debt: int
    savings: int
    needs: int
    wants: int


def _whole_units_down(amount: int, currency: str) -> int:
    unit = 10 ** exponent_for(currency)
    return (amount // unit) * unit


def _scaled(extras: dict[str, int], room: int, currency: str) -> dict[str, int]:
    """[extras] scaled down to fit [room], each to a whole unit below."""
    total = sum(extras.values())
    if total <= room or total == 0:
        return dict(extras)
    return {
        slug: _whole_units_down(amount * room // total, currency)
        for slug, amount in extras.items()
    }


def checked(
    env: Envelope,
    proposed: Mapping[str, int],
    reasons: Mapping[str, str | None],
    currency: str,
) -> Checked:
    """The AI's lines, made to fit the envelope.

    Every line is rounded up to a whole unit and held at its commitments; what
    each adds above them is scaled down when wants pass their share or the
    whole passes what is flexible. Whatever is not used goes to savings, so
    lines, debt and savings add up to income exactly.
    """
    extras: dict[str, int] = {}
    for slug, amount in proposed.items():
        if slug not in SPENDING_SLUGS or amount <= 0:
            continue
        line = max(round_up_to_whole_unit(amount, currency), env.floors.get(slug, 0))
        extras[slug] = line - env.floors.get(slug, 0)

    wants = {s: a for s, a in extras.items() if s in WANT_SLUGS}
    wants_room = max(env.wants_cap - sum(env.floors.get(s, 0) for s in WANT_SLUGS), 0)
    extras.update(_scaled(wants, wants_room, currency))
    extras = _scaled(extras, env.flexible, currency)

    slugs = sorted(set(env.floors) | {s for s, a in extras.items() if a > 0})
    lines = [
        Line(slug, env.floors.get(slug, 0) + extras.get(slug, 0), reasons.get(slug))
        for slug in slugs
    ]
    lines = [line for line in lines if line.amount > 0]
    spent = sum(line.amount for line in lines)
    return Checked(
        lines=sorted(lines, key=lambda line: (-line.amount, line.slug)),
        debt=env.debt,
        savings=env.income - spent - env.debt,
        needs=sum(line.amount for line in lines if line.slug in NEED_SLUGS) + env.debt,
        wants=sum(line.amount for line in lines if line.slug in WANT_SLUGS),
    )


# --- Talking to the model ----------------------------------------------------

_DIGIT = re.compile(r"\d")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def figure_free(text: object, limit: int) -> str | None:
    """[text] without any sentence that carries a figure, or None.

    The model is told not to write amounts; this is what makes that a rule
    rather than a hope. A sentence with a digit in it is dropped, never edited.
    """
    if not isinstance(text, str):
        return None
    kept = [
        s.strip()
        for s in _SENTENCE.split(text.strip())
        if s.strip() and not _DIGIT.search(s)
    ]
    out = " ".join(kept)[:limit].strip()
    return out or None


def _answer(raw: str) -> dict:
    """The model's JSON, tolerating a fenced block around it."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise LlmError("the budget suggestion was not JSON")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise LlmError("the budget suggestion was not JSON") from error
    if not isinstance(data, dict):
        raise LlmError("the budget suggestion was not an object")
    return data


def _proposed(
    data: dict, currency: str
) -> tuple[dict[str, int], dict[str, str | None]]:
    amounts: dict[str, int] = {}
    reasons: dict[str, str | None] = {}
    exponent = exponent_for(currency)
    for item in data.get("lines") or []:
        if not isinstance(item, dict):
            continue
        slug = item.get("category")
        if slug not in SPENDING_SLUGS:
            continue
        try:
            value = Decimal(
                str(item.get("amount", "")).replace(",", "").replace("$", "")
            )
        except InvalidOperation:
            continue
        if not value.is_finite() or value < 0:
            continue
        amounts[slug] = int(value * 10**exponent)
        reasons[slug] = figure_free(item.get("reason"), REASON_MAX_CHARS)
    return amounts, reasons


_RULES = f"""You help a person plan a monthly budget in a personal finance app.
Follow the 50/30/20 rule, adjusted: about {NEEDS_PERCENT}% of income for needs,
{WANTS_PERCENT}% for wants, {SAVINGS_PERCENT}% for savings. The app has already
set aside the fixed parts: each category's commitments, the debts' minimum
payments, and savings. You divide only the "flexible" amount across the
spending categories, as monthly amounts.

Needs: {", ".join(NEED_SLUGS)}.
Wants: {", ".join(WANT_SLUGS)}.

Rules:
- Every line is the category's whole monthly amount. A category with a
  commitment must be at least that commitment.
- Your lines above their commitments must add up to no more than "flexible".
- Keep wants within the wants share. If savings_short is true, keep wants lean.
- "usual_monthly_spending" is what the person actually spent in each
  category: the middle of their last three months. When it is given, start
  every one of those categories from its usual amount and move away from it
  only for a reason — to keep wants within their share, to make room for
  savings, or because the usual amount is clearly more than the category needs —
  and say so in its reason. Never leave out a category they usually spend in.
- With no usual spending (a new account), use their estimated monthly
  expenses and sensible amounts for their income.
- Leave out categories they are unlikely to spend in.
- Reasons: one short sentence each, plain words, no amounts, no percentages,
  no digits at all. The note: two sentences at most, no digits.
- Guidance, not financial advice. Never mention products or companies.

Answer with JSON only:
{{"lines": [{{"category": "<slug>", "amount": "<decimal>", "reason": "<text>"}}],
 "note": "<text>"}}
"""

_REVIEW_RULES = (
    _RULES.replace(
        '"note": "<text>"',
        '"advice": "<text>"',
    )
    + """
This is a monthly review. "last_month" shows what each line allowed and what
was spent. Suggest next amounts that fit how the person actually spends —
lower where they spend less, higher where they keep going over and it is a
need — still within the rules above. The advice: two or three sentences, no
digits, on what went over, what was saved, and where money left over is best
put next (the emergency buffer first, then goals by priority, then paying
down the highest-rate debt), pointing them to Your plan.
"""
)


def _money(minor: int, currency: str) -> str:
    return from_minor_units(minor, currency)


@dataclass
class Inputs:
    currency: str
    income: int
    expense_estimate: int | None
    floors: dict[str, int]
    debt_minimums: int
    goals_need: int
    goals: list[dict] = field(default_factory=list)
    debts: list[dict] = field(default_factory=list)
    history: dict[str, int] = field(default_factory=dict)
    last_month: list[dict] = field(default_factory=list)
    saved_last_month: int | None = None


def _prompt(inputs: Inputs, env: Envelope) -> str:
    c = inputs.currency
    body = {
        "currency": c,
        "monthly_income": _money(inputs.income, c),
        "estimated_monthly_expenses": (
            _money(inputs.expense_estimate, c) if inputs.expense_estimate else None
        ),
        "commitments_by_category": {s: _money(a, c) for s, a in env.floors.items()},
        "debt_minimum_payments": _money(env.debt, c),
        "savings": _money(env.savings, c),
        "savings_short": env.savings_short,
        "flexible": _money(env.flexible, c),
        "needs_share": _money(env.needs_cap, c),
        "wants_share": _money(env.wants_cap, c),
        "goals": inputs.goals,
        "debts": inputs.debts,
        "usual_monthly_spending": {s: _money(a, c) for s, a in inputs.history.items()},
    }
    if inputs.last_month:
        body["last_month"] = inputs.last_month
        body["saved_last_month"] = (
            _money(inputs.saved_last_month, c)
            if inputs.saved_last_month is not None
            else None
        )
    return json.dumps(body, indent=1)


# --- Reading the household ---------------------------------------------------


async def _inputs(
    session: AsyncSession, household_id: uuid.UUID, currency: str, today: date
) -> Inputs:
    from app.services.budget import _history, _minimums, median  # its own rules

    profile = await session.scalar(
        select(FinancialProfile).where(FinancialProfile.household_id == household_id)
    )
    if (
        profile is None
        or profile.currency != currency
        or not profile.monthly_income_minor_units
    ):
        raise NoIncome()

    shared = await commitments.shared_categories(session)
    slug_by_id = {id_: slug for slug, id_ in shared.items()}
    floors = {
        slug_by_id[key]: amount
        for key, amount in (
            await commitments.floors(session, household_id, currency)
        ).items()
        if key in slug_by_id
    }

    month = today.replace(day=1)
    window, history = await _history(session, household_id, currency, month)
    minimums = await _minimums(session, household_id, currency, window)
    # Each category's usual monthly spending: the median of the window, rounded
    # up — the automatic budget's own rule (`budget.suggest`). A household's
    # own category with a shared slug counts toward that slug's months.
    months_by_slug: dict[str, list[int]] = {}
    for ref, totals in history.items():
        if ref.slug in SPENDING_SLUGS:
            running = months_by_slug.setdefault(ref.slug, [0] * len(totals))
            months_by_slug[ref.slug] = [
                a + b for a, b in zip(running, totals, strict=True)
            ]
    medians = {
        slug: usual
        for slug, totals in months_by_slug.items()
        if (usual := round_up_to_whole_unit(median(totals), currency)) > 0
    }

    goals = []
    need = 0
    for goal in (
        await session.scalars(
            select(Goal).where(
                Goal.household_id == household_id, Goal.currency == currency
            )
        )
    ).all():
        if goal.achieved_at is not None:
            continue
        projection = project(
            goal.target_minor_units,
            goal.saved_minor_units,
            goal.target_date,
            goal.monthly_contribution_minor_units,
            today,
        )
        asks = monthly_need(goal, projection)
        need += asks
        goals.append(
            {
                "kind": goal.kind.value if goal.kind else "other",
                "priority": goal.priority,
                "monthly_need": _money(asks, currency),
                "still_needed": _money(projection.remaining, currency),
            }
        )

    debts = [
        {
            "kind": debt.kind.value,
            "rate_percent": (
                str(Decimal(debt.interest_rate_bps) / 100)
                if debt.interest_rate_bps is not None
                else None
            ),
            "minimum": _money(debt.minimum_payment_minor_units or 0, currency),
            "balance": _money(debt.balance_minor_units, currency),
        }
        for debt in (
            await session.scalars(
                select(Debt).where(
                    Debt.household_id == household_id, Debt.currency == currency
                )
            )
        ).all()
    ]
    return Inputs(
        currency=currency,
        income=profile.monthly_income_minor_units,
        expense_estimate=profile.monthly_expense_minor_units,
        floors=floors,
        debt_minimums=minimums,
        goals_need=need,
        goals=goals,
        debts=debts,
        history=medians,
    )


async def _last_month(
    session: AsyncSession, household_id: uuid.UUID, currency: str, today: date
) -> tuple[list[dict], int | None]:
    """Last month's lines against what was spent, and what was saved.

    Read through `budget_for`, exactly as opening that month would: a month
    never opened is built the same way first.
    """
    from app.services.budget import budget_for

    previous = months_back(today.replace(day=1), 2)[0]
    view = await budget_for(
        session, household_id, currency, previous, today, ready=True
    )
    rows = [
        {
            "category": line.slug,
            "allowed": _money(line.allocated, currency),
            "spent": _money(line.spent, currency),
        }
        for line in view.lines
        if line.slug in SPENDING_SLUGS
    ]
    rows += [
        {
            "category": u.slug,
            "allowed": _money(0, currency),
            "spent": _money(u.spent, currency),
        }
        for u in view.unbudgeted
        if u.slug in SPENDING_SLUGS
    ]
    figures = (await figures_by_month(session, household_id, currency, [previous])).get(
        previous
    )
    saved = (figures.invested - figures.withdrawn) if figures else None
    return rows, saved


# --- Asking, checking, storing -----------------------------------------------


async def stored(
    session: AsyncSession, household_id: uuid.UUID, month: date, kind: str
) -> AiBudgetSuggestion | None:
    return await session.scalar(
        select(AiBudgetSuggestion).where(
            AiBudgetSuggestion.household_id == household_id,
            AiBudgetSuggestion.month == month,
            AiBudgetSuggestion.kind == kind,
        )
    )


async def _ask(client: LlmClient, system: str, user: str) -> dict:
    raw = await client.complete(
        system=system, user=user, max_output_tokens=MAX_OUTPUT_TOKENS
    )
    return _answer(raw)


async def _ids(session: AsyncSession) -> dict[str, uuid.UUID]:
    result = await session.execute(
        select(Category.slug, Category.id).where(
            Category.household_id.is_(None),
            Category.slug.in_((*SPENDING_SLUGS, DEBT_SLUG)),
        )
    )
    return dict(result.all())


DEBT_SLUG = "debt_payment"


def _keep_usual(amounts: dict[str, int], usual: Mapping[str, int]) -> None:
    """A category the person usually spends in is never dropped: one the
    model left out is put back at its usual amount, and `checked` still makes
    it fit."""
    for slug, amount in usual.items():
        amounts.setdefault(slug, amount)


def _rows(
    result: Checked,
    env: Envelope,
    ids: Mapping[str, uuid.UUID],
    usual: Mapping[str, int] | None = None,
) -> list[dict]:
    """The stored lines: spending, then the Debt line (its minimums, which the
    budget's own rule sets — never written from here)."""
    rows = [
        {
            "category_id": str(ids[line.slug]),
            "slug": line.slug,
            "amount_minor_units": line.amount,
            "committed_minor_units": env.floors.get(line.slug, 0),
            # What the person usually spends here, when there is history.
            "usual_minor_units": (usual or {}).get(line.slug),
            "reason": line.reason,
        }
        for line in result.lines
        if line.slug in ids
    ]
    if result.debt > 0 and DEBT_SLUG in ids:
        rows.append(
            {
                "category_id": str(ids[DEBT_SLUG]),
                "slug": DEBT_SLUG,
                "amount_minor_units": result.debt,
                "committed_minor_units": result.debt,
                "reason": None,
            }
        )
    return rows


async def suggest_setup(
    session: AsyncSession,
    client: LlmClient,
    household_id: uuid.UUID,
    currency: str,
    today: date,
) -> tuple[AiBudgetSuggestion, Checked]:
    """A first budget from setup's answers, stored for this month.

    Asking again makes a new suggestion until `MAX_SETUP_ATTEMPTS`; after
    that, `TooManyAttempts`.
    """
    month = today.replace(day=1)
    row = await stored(session, household_id, month, SETUP)
    if row is not None and row.attempts >= MAX_SETUP_ATTEMPTS:
        raise TooManyAttempts()
    inputs = await _inputs(session, household_id, currency, today)
    env = envelope(
        inputs.income, inputs.floors, inputs.debt_minimums, inputs.goals_need, currency
    )
    data = await _ask(client, _RULES, _prompt(inputs, env))
    amounts, reasons = _proposed(data, currency)
    _keep_usual(amounts, inputs.history)
    result = checked(env, amounts, reasons, currency)
    rows = _rows(result, env, await _ids(session), inputs.history)
    note = figure_free(data.get("note"), ADVICE_MAX_CHARS)
    if row is None:
        row = AiBudgetSuggestion(
            household_id=household_id,
            month=month,
            kind=SETUP,
            currency=currency,
            attempts=0,
        )
        session.add(row)
    row.lines = rows
    row.savings_minor_units = result.savings
    row.advice = note
    row.model = client.model
    row.attempts += 1
    await session.flush()
    log.info(
        "ai_budget_suggested",
        kind=SETUP,
        lines=len(rows),
        attempt=row.attempts,
        prompt_version=PROMPT_VERSION,
        model=client.model,
    )
    return row, result


async def review(
    session: AsyncSession,
    client: LlmClient,
    household_id: uuid.UUID,
    currency: str,
    today: date,
) -> AiBudgetSuggestion:
    """This month's review: suggested lines from how last month went, and
    advice. Once a month — a second ask returns the stored one."""
    month = today.replace(day=1)
    row = await stored(session, household_id, month, REVIEW)
    if row is not None:
        return row
    inputs = await _inputs(session, household_id, currency, today)
    inputs.last_month, inputs.saved_last_month = await _last_month(
        session, household_id, currency, today
    )
    env = envelope(
        inputs.income, inputs.floors, inputs.debt_minimums, inputs.goals_need, currency
    )
    data = await _ask(client, _REVIEW_RULES, _prompt(inputs, env))
    amounts, reasons = _proposed(data, currency)
    _keep_usual(amounts, inputs.history)
    result = checked(env, amounts, reasons, currency)
    row = AiBudgetSuggestion(
        household_id=household_id,
        month=month,
        kind=REVIEW,
        currency=currency,
        lines=_rows(result, env, await _ids(session), inputs.history),
        savings_minor_units=result.savings,
        advice=figure_free(data.get("advice"), ADVICE_MAX_CHARS),
        model=client.model,
        attempts=1,
    )
    session.add(row)
    await session.flush()
    log.info(
        "ai_budget_suggested",
        kind=REVIEW,
        lines=len(row.lines),
        prompt_version=PROMPT_VERSION,
        model=client.model,
    )
    return row


def totals(rows: Sequence[dict]) -> tuple[int, int]:
    """(needs, wants) of stored lines, the Debt line counted as a need."""
    needs = sum(
        r["amount_minor_units"] for r in rows if r["slug"] in (*NEED_SLUGS, DEBT_SLUG)
    )
    wants = sum(r["amount_minor_units"] for r in rows if r["slug"] in WANT_SLUGS)
    return needs, wants


def kind_of(slug: str) -> str:
    return "want" if slug in WANT_SLUGS else "need"
