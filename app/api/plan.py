"""`/plan` — where the money left over goes, debt payoff first (#76).

Gated by `debt_plan`. Guidance, not advice (PRD §1): the figures are
deterministic arithmetic, "Save or invest" names no product, and the app links
the regional disclaimer beside them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import ai_budget as ai_budget_api
from app.api.deps import current_identity
from app.config import get_settings
from app.core.money import MoneyError, from_minor_units, to_minor_units
from app.db import get_session
from app.models.categorization import Category
from app.models.enums import PlanStrategy
from app.models.planning import Debt
from app.schemas.plan import (
    DiagnosticIn,
    DiagnosticOut,
    DiagnosticsOut,
    EmergencyShareOut,
    GoalShareOut,
    LimitOut,
    PlanDebtOut,
    PlanOut,
    PreferencesIn,
    PreferencesOut,
    SplitEditIn,
    SplitOut,
    SuggestedOut,
    TotalsOut,
    WeightOut,
)
from app.services import budget as budget_service
from app.services import goals as goal_service
from app.services import plan as math
from app.services import plan_diagnostics as diagnostics
from app.services import plan_split
from app.services import plan_view as service
from app.services.capabilities import currency_for, require_feature
from app.services.conflicts import log_conflict
from app.services.dashboard import DEBT_PAYMENT_SLUG, SAVINGS_SLUG, parse_month
from app.services.financial_setup import SetupValidationError, rate_bps, rate_percent
from app.services.goals import GOAL_LIMIT, GoalLimitReached
from app.services.identity import ResolvedIdentity
from app.services.learning import learning_state
from app.services.llm import LlmError, build_client

FEATURE = "debt_plan"
GOAL_LIMIT_REACHED = "goal_limit_reached"

router = APIRouter(
    prefix="/plan",
    tags=["plan"],
    dependencies=[Depends(require_feature(FEATURE))],
)


def _today() -> date:
    """UTC, as the dashboard, budget and goals count days."""
    return datetime.now(UTC).date()


def _unprocessable(code: str, field: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": code, "field": field, "message": message},
    )


def _month_label(month: date | None) -> str | None:
    return None if month is None else f"{month.year:04d}-{month.month:02d}"


def _percent(bps: int) -> str:
    return rate_percent(bps) or "0.00"


def _required_percent(raw: str, field: str) -> int:
    try:
        value = rate_bps(raw, field)
    except SetupValidationError as error:
        raise _unprocessable("invalid_percent", error.field, error.message) from error
    if value is None:
        raise _unprocessable("invalid_percent", field, "A percentage is needed.")
    return value


def _weights_out(weights: tuple[math.Weight, ...]) -> list[WeightOut]:
    return [WeightOut(category=w.category, percent=_percent(w.bps)) for w in weights]


def _goal_share(weights: tuple[math.Weight, ...]) -> int:
    return next((w.bps for w in weights if w.category == math.GOALS), 0)


def _preferences_out(view: service.PlanView, currency: str) -> PreferencesOut:
    """The person's choices — the suggestions until they save some."""

    def money(minor: int | None) -> str | None:
        return None if minor is None else from_minor_units(minor, currency)

    prefs, hint = view.preferences, view.suggested
    return PreferencesOut(
        saved=view.saved,
        buffer=None,
        buffer_goal_id=None,
        goal_share_percent=_percent(_goal_share(prefs.weights)),
        rate_line_percent=_percent(prefs.rate_line_bps),
        strategy=PlanStrategy(prefs.strategy),
        suggested=SuggestedOut(
            buffer=money(hint.buffer),
            goal_share_percent=_percent(_goal_share(hint.weights)),
            rate_line_percent=_percent(hint.rate_line_bps),
            strategy=hint.strategy,
            weights=_weights_out(hint.weights),
        ),
        weights=_weights_out(prefs.weights),
        has_emergency_fund=view.emergency is not None,
    )


def _weights_in(
    body: PreferencesIn, has_emergency_fund: bool
) -> tuple[math.Weight, ...] | None:
    """The choices sent, checked: known categories, each once, percentages
    adding up to exactly 100. The Emergency fund only with its goal."""
    if body.weights is None:
        return None
    weights: list[math.Weight] = []
    seen: set[str] = set()
    for item in body.weights:
        if item.category not in math.CATEGORIES or item.category in seen:
            raise _unprocessable(
                "invalid_weights", "weights", f"{item.category!r} is not a choice."
            )
        if item.category == math.EMERGENCY and not has_emergency_fund:
            raise _unprocessable(
                "invalid_weights",
                "weights",
                "Add an Emergency fund goal before giving it a share.",
            )
        seen.add(item.category)
        weights.append(
            math.Weight(item.category, _required_percent(item.percent, "weights"))
        )
    if sum(w.bps for w in weights) != 10_000:
        raise _unprocessable(
            "invalid_weights", "weights", "The percentages must add up to 100."
        )
    return tuple(weights)


@router.get("/preferences", response_model=PreferencesOut)
async def read_preferences(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> PreferencesOut:
    """The person's choices, or the suggestions the plan runs on until they
    save some."""
    household = identity.household
    today = _today()
    currency = await currency_for(session, household)
    view = await service.build(session, household.id, currency, today, today)
    await session.commit()
    return _preferences_out(view, currency)


@router.put("/preferences", response_model=PreferencesOut)
async def save_preferences(
    body: PreferencesIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> PreferencesOut:
    """Keep the person's choices. A buffer makes or updates their
    emergency-fund goal; no buffer leaves the goal and stops using it."""
    household = identity.household
    today = _today()
    currency = await currency_for(session, household)
    buffer: int | None = None
    if body.buffer is not None:
        try:
            buffer = to_minor_units(body.buffer, currency)
        except MoneyError as error:
            raise _unprocessable("invalid_amount", "buffer", str(error)) from error
        if buffer <= 0:
            raise _unprocessable(
                "invalid_amount", "buffer", "A buffer is more than zero, or none."
            )
    emergency = await service.emergency_goal(session, household.id)
    weights = _weights_in(
        body, emergency is not None and emergency.currency == currency
    )
    try:
        await service.save_preferences(
            session,
            household.id,
            currency,
            buffer=buffer,
            goal_share_bps=(
                None
                if body.goal_share_percent is None
                else _required_percent(body.goal_share_percent, "goal_share_percent")
            ),
            rate_line_bps=(
                None
                if body.rate_line_percent is None
                else _required_percent(body.rate_line_percent, "rate_line_percent")
            ),
            strategy=body.strategy,
            today=today,
            weights=weights,
        )
    except GoalLimitReached as error:
        # The id before the rollback: afterwards the household is expired, and
        # reading it would need a load this async session cannot make here.
        household_id = str(household.id)
        await session.rollback()
        log_conflict(
            GOAL_LIMIT_REACHED, "buffer_goal_over_limit", household_id=household_id
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": GOAL_LIMIT_REACHED,
                "message": f"You already have {GOAL_LIMIT} goals in progress.",
                "limit": GOAL_LIMIT,
            },
        ) from error
    view = await service.build(session, household.id, currency, today, today)
    await session.commit()
    return _preferences_out(view, currency)


async def _rates(
    session: AsyncSession, household_id: uuid.UUID, currency: str, month: date
) -> list[dict]:
    """The debts as the AI is told them: kind, rate, balance, minimum — no names."""
    debts = (
        await session.scalars(
            select(Debt).where(
                Debt.household_id == household_id, Debt.currency == currency
            )
        )
    ).all()
    out = []
    for debt in debts:
        rate = math.rate_on(service.terms_of(debt), month)
        out.append(
            {
                "kind": debt.kind.value,
                "rate_percent": rate_percent(rate),
                "balance": from_minor_units(debt.balance_minor_units, currency),
                "minimum": from_minor_units(
                    debt.minimum_payment_minor_units or 0, currency
                ),
            }
        )
    return out


def _advice_settings():
    settings = get_settings()
    return (
        settings.model_copy(update={"llm_model": settings.llm_advice_model})
        if settings.llm_advice_model
        else settings
    )


async def _decider(
    session: AsyncSession,
    identity: ResolvedIdentity,
    currency: str,
    month: date,
    *,
    ask_ai: bool = True,
):
    """The split as the AI proposes it, kept for the month; by the rules when
    the AI may not be asked. [ask_ai] False reads without asking: the kept
    split while it still fits, else the rules."""
    household = identity.household

    async def may_ask() -> bool:
        if not ask_ai:
            return False
        try:
            await ai_budget_api._may_ask(session, identity.user)
        except HTTPException:
            return False
        return True

    settings = _advice_settings()
    return plan_split.decider(
        session,
        household.id,
        currency,
        month,
        await _rates(session, household.id, currency, month),
        lambda: build_client(settings),
        may_ask,
    )


@router.get("", response_model=PlanOut)
async def read_plan(
    month: str | None = Query(
        default=None, description="YYYY-MM; this month if omitted"
    ),
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> PlanOut:
    return await plan_for(identity, session, month)


async def plan_for(
    identity: ResolvedIdentity,
    session: AsyncSession,
    month: str | None,
    *,
    ask_ai: bool = True,
) -> PlanOut:
    """[month]'s plan. The AI coach reads it with [ask_ai] False: a question
    about the plan must not set off a second model call to split it."""
    household = identity.household
    today = _today()
    try:
        when = parse_month(month) if month else today.replace(day=1)
    except ValueError as error:
        raise _unprocessable("invalid_month", "month", str(error)) from error
    currency = await currency_for(session, household)
    view = await service.build(
        session,
        household.id,
        currency,
        when,
        today,
        decide=await _decider(
            session, identity, currency, when.replace(day=1), ask_ai=ask_ai
        ),
    )
    await session.commit()
    return _plan_out(view, currency)


def _plan_out(view: service.PlanView, currency: str) -> PlanOut:
    def money(minor: int | None) -> str | None:
        return None if minor is None else from_minor_units(minor, currency)

    outcomes = {o.id: o for o in view.schedule.debts}
    debts = []
    for debt in view.debts:
        outcome = outcomes[debt.id]
        terms = service.terms_of(debt)
        rate = math.rate_on(terms, view.month)
        debts.append(
            PlanDebtOut(
                debt_id=debt.id,
                name=debt.name,
                kind=debt.kind,
                balance=from_minor_units(debt.balance_minor_units, currency),
                minimum_payment=money(debt.minimum_payment_minor_units),
                rate_percent=rate_percent(rate),
                order=outcome.order,
                minimum_only=outcome.minimum_only,
                missing_rate=outcome.missing_rate,
                gets_extra_now=view.focus == debt.id,
                paid_off=_month_label(outcome.paid_off),
                never_paid_off=outcome.never_paid_off,
                interest=from_minor_units(outcome.interest, currency),
            )
        )

    chosen = view.schedule.total_interest
    minimums = view.minimums_only.total_interest
    other = view.other_strategy.total_interest
    prefs = view.preferences
    return PlanOut(
        month=_month_label(view.month),
        currency=currency,
        status="ready" if view.learning.ready else "learning",
        leftover=from_minor_units(view.leftover, currency),
        shortfall=money(view.shortfall),
        preferences=_preferences_out(view, currency),
        split=SplitOut(
            buffer=from_minor_units(view.split.buffer, currency),
            buffer_saved=None,
            buffer_target=None,
            goals=[
                GoalShareOut(
                    goal_id=goal_id,
                    name=view.goal_names.get(goal_id, ""),
                    amount=from_minor_units(amount, currency),
                )
                for goal_id, amount in view.split.goals
            ],
            extra_debt=from_minor_units(view.split.extra_debt, currency),
            save_or_invest=from_minor_units(view.split.save_or_invest, currency),
            emergency_fund=(
                EmergencyShareOut(
                    goal_id=view.emergency.id,
                    name=view.emergency.name,
                    amount=from_minor_units(view.split.emergency, currency),
                    still_needed=from_minor_units(
                        view.limits.emergency[1] if view.limits.emergency else 0,
                        currency,
                    ),
                )
                if view.emergency is not None
                else None
            ),
            source=view.decided.source if view.decided else "rules",
            why=view.decided.why if view.decided else None,
            edited=view.decided.edited if view.decided else False,
            limits=_limits_out(view, currency),
        ),
        debts=debts,
        totals=TotalsOut(
            total_interest=money(chosen),
            debt_free=_month_label(view.schedule.debt_free),
            minimums_only_interest=money(minimums),
            interest_saved=(
                money(minimums - chosen)
                if chosen is not None and minimums is not None
                else None
            ),
            other_strategy=PlanStrategy(
                math.SNOWBALL if prefs.strategy == math.AVALANCHE else math.AVALANCHE
            ),
            other_strategy_difference=(
                money(other - chosen)
                if chosen is not None and other is not None
                else None
            ),
        ),
    )


def _limits_out(view: service.PlanView, currency: str) -> list[LimitOut]:
    """The most each row of "Where it goes" can be set to."""
    total = view.split.total - view.split.buffer

    def most(cap: int | None) -> str:
        return from_minor_units(total if cap is None else min(cap, total), currency)

    out = [LimitOut(row=math.DEBT, most=most(view.limits.debt))]
    if view.limits.emergency is not None:
        out.append(LimitOut(row=math.EMERGENCY, most=most(view.limits.emergency[1])))
    out += [
        LimitOut(row="goal", goal_id=g.id, most=most(g.remaining))
        for g in view.limits.goals
    ]
    out.append(LimitOut(row=math.SAVE_OR_INVEST, most=most(None)))
    return out


async def _system_category(session: AsyncSession, slug: str):
    ids = await budget_service._system_ids(session)
    return await session.get(Category, ids[slug])


async def _set_budget_line(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    month: date,
    today: date,
    slug: str,
    amount: int,
) -> None:
    learning = await learning_state(session, household_id, currency, today)
    await budget_service.set_line(
        session,
        household_id,
        currency,
        month,
        today,
        await _system_category(session, slug),
        amount,
        ready=learning.ready,
    )


@router.put("/split", response_model=PlanOut)
async def edit_split(
    body: SplitEditIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> PlanOut:
    """Set one row of this month's "Where it goes" by hand; the others
    rebalance by priority so the whole stays the money left over. With
    `apply`, the amount is also put where it belongs: the goal's monthly
    amount, the Budget's Debt line (on top of the minimums), or the Budget's
    savings target."""
    household = identity.household
    today = _today()
    month = today.replace(day=1)
    currency = await currency_for(session, household)
    if body.row not in (math.EMERGENCY, math.DEBT, math.SAVE_OR_INVEST, "goal"):
        raise _unprocessable("invalid_row", "row", f"{body.row!r} is not a row.")
    if body.row == "goal" and body.goal_id is None:
        raise _unprocessable("invalid_row", "goal_id", "Say which goal.")
    try:
        amount = to_minor_units(body.amount, currency)
    except MoneyError as error:
        raise _unprocessable("invalid_amount", "amount", str(error)) from error

    decide = await _decider(session, identity, currency, month)
    view = await service.build(
        session, household.id, currency, month, today, decide=decide
    )
    row = math.GOALS if body.row == "goal" else body.row
    try:
        changed = math.edited(
            view.split, view.preferences, view.limits, row, amount, body.goal_id
        )
    except ValueError as error:
        raise _unprocessable(
            "over_limit", "amount", "More than that row can take this month."
        ) from error

    if body.apply:
        if body.row in (math.EMERGENCY, "goal"):
            goal_id = view.emergency.id if body.row == math.EMERGENCY else body.goal_id
            goal = await goal_service.owned(session, household.id, goal_id)
            if goal is not None:
                await goal_service.edit(
                    session, goal, {"monthly_contribution_minor_units": amount}, today
                )
        elif body.row == math.DEBT:
            budget = await budget_service.budget_for(
                session,
                household.id,
                currency,
                month,
                today,
                ready=view.learning.ready,
            )
            floor = budget.debt.suggested if budget.debt is not None else view.minimums
            await _set_budget_line(
                session,
                household.id,
                currency,
                month,
                today,
                DEBT_PAYMENT_SLUG,
                floor + amount,
            )
        else:
            await _set_budget_line(
                session, household.id, currency, month, today, SAVINGS_SLUG, amount
            )

    rates = await _rates(session, household.id, currency, month)

    async def keep_edit(leftover, preferences, limits):
        return await plan_split.remember_edit(
            session,
            household.id,
            currency,
            month,
            rates,
            leftover,
            preferences,
            limits,
            changed,
        )

    view = await service.build(
        session, household.id, currency, month, today, decide=keep_edit
    )
    await session.commit()
    return _plan_out(view, currency)


# --- Diagnostics ---------------------------------------------------------------


async def _savings_defaults(
    session: AsyncSession, household_id: uuid.UUID, currency: str, today: date
) -> tuple[int, int]:
    """(target, saved): what the person last ran with, else the Emergency
    fund goal's, else the plan's suggested buffer and nothing saved."""
    remembered = await diagnostics.remembered(session, household_id)
    if remembered:
        return remembered.get("savings_target", 0), remembered.get("savings_now", 0)
    row = await service.preference_row(session, household_id)
    goal = None
    if row is not None and row.buffer_goal_id is not None:
        goal = await goal_service.owned(session, household_id, row.buffer_goal_id)
    if goal is None:
        goal = await service._emergency_goal(session, household_id)
    if goal is not None and goal.currency == currency:
        return goal.target_minor_units, goal.saved_minor_units
    hint = await service.suggested(session, household_id, currency, today)
    return hint.buffer or 0, 0


def _diagnostic_out(row, stored: bool, disclaimer: str | None) -> DiagnosticOut:
    figures, advice = row.figures, row.advice
    return DiagnosticOut(
        kind=row.kind,
        made_at=row.created_at.isoformat(),
        stored=stored,
        currency=row.currency,
        **{k: v for k, v in figures.items() if k in DiagnosticOut.model_fields},
        **{k: v for k, v in advice.items() if k in DiagnosticOut.model_fields},
        disclaimer=disclaimer if row.kind == diagnostics.INVEST else None,
    )


async def _disclaimer(session: AsyncSession, household) -> str | None:
    found = await goal_service.regional_disclaimer(session, household)
    return found.body if found else None


@router.get("/diagnostics", response_model=DiagnosticsOut)
async def read_diagnostics(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> DiagnosticsOut:
    """The last run of each diagnostic, and what the form starts with."""
    household = identity.household
    today = _today()
    currency = await currency_for(session, household)
    target, saved = await _savings_defaults(session, household.id, currency, today)
    monthly = await _monthly_savings_default(session, household.id, currency, today)
    disclaimer = await _disclaimer(session, household)
    out = {}
    left = {}
    for kind in diagnostics.KINDS:
        row = await diagnostics.latest(session, household.id, kind)
        out[kind] = _diagnostic_out(row, True, disclaimer) if row else None
        used = await diagnostics.runs_this_month(
            session, household.id, kind, today.replace(day=1)
        )
        left[kind] = max(diagnostics.MAX_RUNS_A_MONTH - used, 0)
    return DiagnosticsOut(
        allocation=out[diagnostics.ALLOCATION],
        invest=out[diagnostics.INVEST],
        currency=currency,
        savings_target=from_minor_units(target, currency),
        savings_now=from_minor_units(saved, currency),
        runs_left=left,
        monthly_savings=from_minor_units(monthly, currency),
    )


async def _monthly_savings_default(
    session: AsyncSession, household_id: uuid.UUID, currency: str, today: date
) -> int:
    """The Budget's savings target this month, else what was last run with."""
    learning = await learning_state(session, household_id, currency, today)
    budget = await budget_service.budget_for(
        session,
        household_id,
        currency,
        today.replace(day=1),
        today,
        ready=learning.ready,
    )
    if budget.savings is not None and budget.savings.is_user_set:
        return budget.savings.allocated
    remembered = await diagnostics.remembered(session, household_id)
    return int((remembered or {}).get("monthly_savings", 0) or 0)


def _savings_amount(raw: str | None, currency: str, field: str) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        amount = to_minor_units(raw, currency)
    except MoneyError as error:
        raise _unprocessable("invalid_amount", field, str(error)) from error
    if amount < 0:
        raise _unprocessable("invalid_amount", field, "Savings cannot be negative.")
    return amount


@router.post("/diagnostics/{kind}", response_model=DiagnosticOut)
async def run_diagnostic(
    kind: str,
    body: DiagnosticIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> DiagnosticOut:
    """Run a diagnostic on the person's figures. The same figures as last time
    return the stored run; otherwise a fresh one, up to four a month."""
    if kind not in diagnostics.KINDS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"}
        )
    household = identity.household
    today = _today()
    currency = await currency_for(session, household)
    learning = await learning_state(session, household.id, currency, today)
    if not learning.ready:
        log_conflict(
            "still_learning", "diagnostic_before_ready", household_id=str(household.id)
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "still_learning",
                "message": "Diagnostics need a full month of your spending first.",
            },
        )
    target_default, saved_default = await _savings_defaults(
        session, household.id, currency, today
    )
    target = _savings_amount(body.savings_target, currency, "savings_target")
    saved = _savings_amount(body.savings_now, currency, "savings_now")
    target = target_default if target is None else target
    saved = saved_default if saved is None else saved
    monthly = _savings_amount(body.monthly_savings, currency, "monthly_savings")
    if monthly is None:
        monthly = await _monthly_savings_default(session, household.id, currency, today)

    await ai_budget_api._may_ask(session, identity.user)
    advice_settings = _advice_settings()
    try:
        row, stored = await diagnostics.run(
            session,
            lambda: build_client(advice_settings),
            household,
            currency,
            today,
            kind,
            target,
            saved,
            monthly,
        )
    except diagnostics.LimitReached as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "diagnostic_limit_reached",
                "message": (
                    "That's all the fresh runs this month. The last answer stays."
                ),
            },
        ) from error
    except LlmError as error:
        raise ai_budget_api._unavailable() from error
    if kind == diagnostics.ALLOCATION and body.monthly_savings is not None:
        # What they save each month is the Budget's savings target (redesign,
        # 2026-10-31): the figure typed here is set there too.
        await _set_budget_line(
            session,
            household.id,
            currency,
            today.replace(day=1),
            today,
            SAVINGS_SLUG,
            monthly,
        )
    await session.commit()
    return _diagnostic_out(row, stored, await _disclaimer(session, household))
