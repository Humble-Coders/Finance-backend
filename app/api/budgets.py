"""`/budgets` — a month's budget, generated from real spending (PRD F4).

Gated by `auto_budget`: the capabilities payload decides whether a client
shows budgets, and `require_feature` decides whether this answers at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.core.money import MoneyError, from_minor_units, to_minor_units
from app.db import get_session
from app.models.categorization import Category
from app.schemas.budget import (
    BudgetLineIn,
    BudgetLineOut,
    BudgetOut,
    LearningNeedsOut,
    LearningOut,
)
from app.services import budget as service
from app.services.capabilities import currency_for, require_feature
from app.services.dashboard import parse_month
from app.services.identity import ResolvedIdentity
from app.services.learning import LearningState, learning_state

FEATURE = "auto_budget"

# The largest line a BIGINT column holds. Anything above it is not a budget
# anybody means, and without this it reaches the database as a 500.
MAX_LINE_MINOR_UNITS = 2**63 - 1

router = APIRouter(
    prefix="/budgets",
    tags=["budgets"],
    dependencies=[Depends(require_feature(FEATURE))],
)


def _today() -> date:
    return datetime.now(UTC).date()


def _month(raw: str) -> date:
    """`YYYY-MM`, not after the current UTC month.

    UTC for the reason `/dashboard` gives. A future month is refused rather
    than budgeted: there is nothing to regenerate it from that the current
    month does not already use, and a client asking for one has a bug.
    """
    try:
        month = parse_month(raw)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_month", "message": "Expected YYYY-MM."},
        ) from error
    if month > _today().replace(day=1):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "month_in_future", "message": "That month has not begun."},
        )
    return month


async def _visible_category(
    session: AsyncSession, household_id: uuid.UUID, category_id: uuid.UUID
) -> Category:
    """A system category or the household's own, else 404.

    404 rather than 403, and the same 404 as a category that does not exist:
    anything else would tell a caller which ids belong to someone.
    """
    category = await session.scalar(
        select(Category).where(
            Category.id == category_id,
            or_(
                Category.household_id.is_(None),
                Category.household_id == household_id,
            ),
        )
    )
    if category is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"}
        )
    return category


def _not_budgetable(slug: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "code": "not_budgetable",
            "field": "category_id",
            "message": f"{slug} is money moving, not money spent.",
        },
    )


def _out(view: service.BudgetView, learning: LearningState) -> BudgetOut:
    """One shape whether or not the household is still learning.

    While learning, `status` says so, `learning` carries the progress, and
    the lines are only the ones the user set by hand — nothing generated.
    """
    currency = view.currency

    def money(minor: int) -> str:
        return from_minor_units(minor, currency)

    def line(item: service.LineView | None) -> BudgetLineOut | None:
        if item is None:
            return None
        return BudgetLineOut(
            category_id=item.category_id,
            slug=item.slug,
            name=item.name,
            suggested=money(item.suggested),
            allocated=money(item.allocated),
            is_user_set=item.is_user_set,
            spent=money(item.spent),
        )

    return BudgetOut(
        status="ready" if learning.ready else "learning",
        month=view.month,
        currency=currency,
        learning=None
        if learning.ready
        else LearningOut(
            ready=False,
            complete_months=learning.complete_months,
            transactions=learning.transactions,
            needs=LearningNeedsOut(**learning.needs),
        ),
        expected_income=money(view.expected_income),
        lines=[line(item) for item in view.lines],
        savings=line(view.savings),
        debt=line(view.debt),
        total_allocated=money(view.total_allocated),
        total_spent=money(view.total_spent),
        shortfall=None if view.shortfall is None else money(view.shortfall),
        uncategorised_spent=money(view.uncategorised_spent),
        uncategorised_count=view.uncategorised_count,
    )


def _invalid_amount(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": "invalid_amount", "field": "amount", "message": message},
    )


@router.get("/{month}", response_model=BudgetOut)
async def read_budget(
    month: str,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> BudgetOut:
    """The month's budget, or how far the household is from having one.

    "Still learning" is a 200: it is a state of the account, not an error.
    The lines the user has set by hand show either way.
    """
    when = _month(month)
    today = _today()
    household = identity.household
    currency = await currency_for(session, household)
    learning = await learning_state(session, household.id, currency, today)
    view = await service.budget_for(
        session, household.id, currency, when, today, ready=learning.ready
    )
    await session.commit()
    return _out(view, learning)


@router.put("/{month}/lines/{category_id}", response_model=BudgetOut)
async def set_line(
    month: str,
    category_id: uuid.UUID,
    body: BudgetLineIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> BudgetOut:
    """Set one line by hand. Regeneration will not move it again."""
    when = _month(month)
    today = _today()
    household = identity.household
    currency = await currency_for(session, household)
    category = await _visible_category(session, household.id, category_id)
    if category.slug in service.NOT_BUDGETABLE:
        raise _not_budgetable(category.slug)
    try:
        amount = to_minor_units(body.amount, currency)
    except MoneyError as error:
        raise _invalid_amount(str(error)) from error
    if amount < 0:
        raise _invalid_amount("A budget line cannot be negative.")
    if amount > MAX_LINE_MINOR_UNITS:
        raise _invalid_amount("That amount is too large.")
    learning = await learning_state(session, household.id, currency, today)
    view = await service.set_line(
        session,
        household.id,
        currency,
        when,
        today,
        category,
        amount,
        ready=learning.ready,
    )
    await session.commit()
    return _out(view, learning)


@router.delete("/{month}/lines/{category_id}/override", response_model=BudgetOut)
async def reset_line(
    month: str,
    category_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> BudgetOut:
    """Put one line back to its suggestion; a hand-added line with none goes."""
    when = _month(month)
    today = _today()
    household = identity.household
    currency = await currency_for(session, household)
    category = await _visible_category(session, household.id, category_id)
    if category.slug in service.NOT_BUDGETABLE:
        raise _not_budgetable(category.slug)
    learning = await learning_state(session, household.id, currency, today)
    view = await service.reset_line(
        session, household.id, currency, when, today, category, ready=learning.ready
    )
    if view is None:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"}
        )
    await session.commit()
    return _out(view, learning)
