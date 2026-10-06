"""`/goals` — savings targets and what each needs (PRD F5).

Gated by `goals`. **Not** gated by the learning threshold: PRD F8 says goals
work immediately. Only the budget comparison waits for enough history, and
says so.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.core.money import MoneyError, from_minor_units, to_minor_units
from app.db import get_session
from app.models.enums import GoalHorizon
from app.models.planning import Goal
from app.schemas.goals import (
    AddMoneyIn,
    GoalIn,
    GoalOrderIn,
    GoalOut,
    GoalPatch,
    GoalsBudgetOut,
    GoalsOut,
)
from app.services import goals as service
from app.services.capabilities import currency_for, require_feature
from app.services.conflicts import log_conflict
from app.services.identity import ResolvedIdentity

FEATURE = "goals"
GOAL_LIMIT_REACHED = "goal_limit_reached"

router = APIRouter(
    prefix="/goals",
    tags=["goals"],
    dependencies=[Depends(require_feature(FEATURE))],
)


def _today() -> date:
    """UTC, the convention `parse_month` and the dashboard use."""
    return datetime.now(UTC).date()


def _unprocessable(
    code: str, field: str | None = None, message: str | None = None
) -> HTTPException:
    detail: dict[str, str] = {"code": code}
    if field:
        detail["field"] = field
    if message:
        detail["message"] = message
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail
    )


def _not_found() -> HTTPException:
    """The same 404 for a goal that is missing and one that is someone else's:
    anything else would tell a caller which ids exist."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"}
    )


def _amount(raw: str, currency: str, field: str, *, positive: bool = False) -> int:
    """A decimal string as minor units, or `422 invalid_amount`."""
    try:
        value = to_minor_units(raw, currency)
    except MoneyError as error:
        raise _unprocessable("invalid_amount", field, str(error)) from error
    if value < 0 or (positive and value == 0):
        raise _unprocessable(
            "invalid_amount",
            field,
            "Must be more than zero." if positive else "Can't be negative.",
        )
    if value > service.MAX_MINOR_UNITS:
        raise _unprocessable("invalid_amount", field, "That amount is too large.")
    return value


def _name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise _unprocessable("invalid_name", "name", "Give the goal a name.")
    return name


def _future(target_date: date | None) -> date | None:
    if target_date is not None and target_date < _today():
        raise _unprocessable(
            "date_in_past", "target_date", "Choose a date from today on."
        )
    return target_date


def _out(goal: Goal, today: date) -> GoalOut:
    projection = service.project(
        goal.target_minor_units,
        goal.saved_minor_units,
        goal.target_date,
        goal.monthly_contribution_minor_units,
        today,
    )
    return _goal_out(goal, projection)


def _goal_out(goal: Goal, projection: service.Projection) -> GoalOut:
    currency = goal.currency

    def money(minor: int | None) -> str | None:
        return None if minor is None else from_minor_units(minor, currency)

    completion = projection.projected_completion
    return GoalOut(
        id=goal.id,
        name=goal.name,
        kind=goal.kind,
        horizon=goal.horizon,
        target=from_minor_units(goal.target_minor_units, currency),
        saved=from_minor_units(goal.saved_minor_units, currency),
        remaining=from_minor_units(projection.remaining, currency),
        target_date=goal.target_date,
        monthly_contribution=money(goal.monthly_contribution_minor_units),
        required_monthly=money(projection.required_monthly),
        projected_completion=None
        if completion is None
        else f"{completion.year:04d}-{completion.month:02d}",
        progress_percent=projection.progress_percent,
        status=projection.status,
        achieved_at=goal.achieved_at,
        priority=goal.priority,
    )


@router.get("", response_model=GoalsOut)
async def list_goals(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> GoalsOut:
    """Every goal in its order, what each needs, and the month's budget beside them."""
    today = _today()
    household = identity.household
    currency = await currency_for(session, household)
    goals = await service.goals_of(session, household.id)
    projected = [
        (
            goal,
            service.project(
                goal.target_minor_units,
                goal.saved_minor_units,
                goal.target_date,
                goal.monthly_contribution_minor_units,
                today,
            ),
        )
        for goal in goals
    ]
    comparison, reason = await service.compare_with_budget(
        session, household, currency, projected, today
    )
    disclaimer = None
    if any(goal.horizon == GoalHorizon.long_term for goal in goals):
        found = await service.regional_disclaimer(session, household)
        disclaimer = found.version if found else None
    # The comparison may have settled this month's budget, as /budgets would.
    await session.commit()
    return GoalsOut(
        goals=[_goal_out(goal, projection) for goal, projection in projected],
        budget=None
        if comparison is None
        else GoalsBudgetOut(
            need=from_minor_units(comparison.need, currency),
            set_aside=from_minor_units(comparison.set_aside, currency),
            shortfall=None
            if comparison.shortfall is None
            else from_minor_units(comparison.shortfall, currency),
        ),
        budget_reason=reason,
        disclaimer_version=disclaimer,
        projection_version=service.PROJECTION_VERSION,
        assumes_growth=service.ASSUMES_GROWTH,
    )


@router.post("", response_model=GoalOut, status_code=status.HTTP_201_CREATED)
async def create_goal(
    body: GoalIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> GoalOut:
    today = _today()
    household = identity.household
    currency = await currency_for(session, household)
    target = _amount(body.target, currency, "target", positive=True)
    saved = _amount(body.saved, currency, "saved") if body.saved is not None else 0
    contribution = (
        _amount(body.monthly_contribution, currency, "monthly_contribution")
        if body.monthly_contribution is not None
        else None
    )
    try:
        goal = await service.create(
            session,
            household.id,
            currency,
            name=_name(body.name),
            kind=body.kind,
            horizon=body.horizon,
            target=target,
            saved=saved,
            target_date=_future(body.target_date),
            monthly_contribution=contribution,
            today=today,
        )
    except service.GoalLimitReached as error:
        # Read before the rollback, which expires the household.
        household_id = str(household.id)
        await session.rollback()
        log_conflict(GOAL_LIMIT_REACHED, "open_goal_limit", household_id=household_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": GOAL_LIMIT_REACHED,
                "limit": service.GOAL_LIMIT,
                "message": "That's the most goals that can be in progress at once.",
            },
        ) from error
    await session.commit()
    return _out(goal, today)


@router.patch("/{goal_id}", response_model=GoalOut)
async def edit_goal(
    goal_id: uuid.UUID,
    body: GoalPatch,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> GoalOut:
    """Change any of the fields sent. Null clears `kind`, `target_date` or
    `monthly_contribution`; the rest cannot be cleared."""
    today = _today()
    household = identity.household
    goal = await service.owned(session, household.id, goal_id)
    if goal is None:
        raise _not_found()
    currency = goal.currency
    sent = body.model_fields_set
    changes: dict[str, object] = {}
    for required in ("name", "horizon", "target", "saved"):
        if required in sent and getattr(body, required) is None:
            raise _unprocessable("invalid_value", required, "This can't be cleared.")
    if "name" in sent:
        changes["name"] = _name(body.name)
    if "kind" in sent:
        changes["kind"] = body.kind
    if "horizon" in sent:
        changes["horizon"] = body.horizon
    if "target" in sent:
        changes["target_minor_units"] = _amount(
            body.target, currency, "target", positive=True
        )
    if "saved" in sent:
        changes["saved_minor_units"] = _amount(body.saved, currency, "saved")
    if "target_date" in sent:
        # Only a date being set is held to "from today on"; leaving an overdue
        # goal's date alone while editing its name is not a new date in the past.
        changes["target_date"] = (
            _future(body.target_date)
            if body.target_date != goal.target_date
            else body.target_date
        )
    if "monthly_contribution" in sent:
        changes["monthly_contribution_minor_units"] = (
            None
            if body.monthly_contribution is None
            else _amount(body.monthly_contribution, currency, "monthly_contribution")
        )
    goal = await service.edit(session, goal, changes, today)
    await session.commit()
    return _out(goal, today)


@router.post("/{goal_id}/add", response_model=GoalOut)
async def add_money(
    goal_id: uuid.UUID,
    body: AddMoneyIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> GoalOut:
    """Add to what is saved, atomically: two adds at once both land."""
    today = _today()
    household = identity.household
    existing = await service.owned(session, household.id, goal_id)
    if existing is None:
        raise _not_found()
    amount = _amount(body.amount, existing.currency, "amount", positive=True)
    try:
        goal = await service.add_money(session, household.id, goal_id, amount, today)
    except service.AddTooLarge as error:
        raise _unprocessable(
            "invalid_amount",
            "amount",
            "That would take the goal past what it can hold.",
        ) from error
    if goal is None:
        raise _not_found()
    await session.commit()
    return _out(goal, today)


@router.put("/order", response_model=GoalsOut)
async def reorder_goals(
    body: GoalOrderIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> GoalsOut:
    """Set the order: exactly the household's goals, each once."""
    household = identity.household
    if not await service.reorder(session, household.id, body.ids):
        await session.rollback()
        raise _unprocessable(
            "order_mismatch",
            "ids",
            "Send every one of your goals, each once.",
        )
    await session.commit()
    return await list_goals(identity, session)


@router.delete("/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_goal(
    goal_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if not await service.remove(session, identity.household.id, goal_id):
        raise _not_found()
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
