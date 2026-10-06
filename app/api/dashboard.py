"""`GET /dashboard` — the monthly overview both apps render.

Computed on the server, not on each platform, for the reason shared logic
exists at all: Android and iOS must not be able to disagree about a number
somebody is making a decision from. A figure derived twice is derived
differently eventually.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.api.health_score import missing_line
from app.core.money import from_minor_units
from app.db import get_session
from app.models.derived import HealthScoreSnapshot
from app.schemas.budget import LearningNeedsOut, LearningOut
from app.schemas.dashboard import (
    AsOfOut,
    CategorySpendOut,
    CommitmentOut,
    DashboardBudgetLineOut,
    DashboardBudgetOut,
    DashboardOut,
    DashboardScoreOut,
    DayPointOut,
    FlowOut,
    MatchOut,
    MonthPointOut,
    StockOut,
)
from app.schemas.health_score import NoticeOut
from app.services import budget as budgets
from app.services import dashboard as service
from app.services import health_score as scores
from app.services.capabilities import currency_for, resolve
from app.services.identity import ResolvedIdentity
from app.services.learning import LearningState, learning_state

router = APIRouter(tags=["dashboard"])
log = structlog.get_logger()

# The features that decide whether the budget and the score are included.
FEATURE_BUDGET = "auto_budget"
FEATURE_SCORE = "health_score"


def _month_or_now(raw: str | None) -> date:
    """`YYYY-MM`, or the current UTC month.

    UTC rather than the household's own timezone, which we do not store.
    Somebody opening the app late on the 31st sees the next month a few hours
    early — the error direction to prefer, since the alternative is showing a
    month that has already ended as if it were still running.
    """
    if raw is None:
        return _today().replace(day=1)
    try:
        return service.parse_month(raw)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_month", "message": "Expected YYYY-MM."},
        ) from error


def _today() -> date:
    return datetime.now(UTC).date()


def _learning_out(learning: LearningState) -> LearningOut:
    return LearningOut(
        ready=learning.ready,
        complete_months=learning.complete_months,
        transactions=learning.transactions,
        needs=LearningNeedsOut(**learning.needs),
    )


def _budget_out(
    view: budgets.BudgetView, learning: LearningState, money
) -> DashboardBudgetOut:
    """4.1's budget for the month, in the order `/budgets` serves it."""

    def line(item: budgets.LineView | None) -> DashboardBudgetLineOut | None:
        if item is None:
            return None
        return DashboardBudgetLineOut(
            category_id=item.category_id,
            slug=item.slug,
            name=item.name,
            allocated=money(item.allocated),
            spent=money(item.spent),
            over=money(max(0, item.spent - item.allocated)),
        )

    return DashboardBudgetOut(
        status="ready" if learning.ready else "learning",
        learning=None if learning.ready else _learning_out(learning),
        lines=[line(item) for item in view.lines],
        savings=line(view.savings),
        debt=line(view.debt),
        total_allocated=money(view.total_allocated),
        total_spent=money(view.total_spent),
        shortfall=None if view.shortfall is None else money(view.shortfall),
    )


async def _last_snapshot(
    session: AsyncSession,
    household_id: uuid.UUID,
    on_or_after: date | None,
    until: date,
) -> HealthScoreSnapshot | None:
    """The household's latest snapshot no later than [until] (and no earlier
    than [on_or_after], when given). A read; never a computation."""
    query = select(HealthScoreSnapshot).where(
        HealthScoreSnapshot.household_id == household_id,
        HealthScoreSnapshot.scored_on <= until,
    )
    if on_or_after is not None:
        query = query.where(HealthScoreSnapshot.scored_on >= on_or_after)
    return await session.scalar(
        query.order_by(HealthScoreSnapshot.scored_on.desc()).limit(1)
    )


async def _score_out(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    month: date,
    today: date,
    learning: LearningState,
) -> DashboardScoreOut:
    """The score for the month shown — which is a day's score, not a month's.

    The current month carries today's, from 4.2's `current_score`, so it
    matches `GET /health-score` exactly (held score and notice included). A
    past month carries the last snapshot on or before its end, read and never
    computed: a past month cannot be re-scored without rewriting history.
    """
    if not learning.ready:
        return DashboardScoreOut(status="learning", learning=_learning_out(learning))

    if month != today.replace(day=1):
        held = await _last_snapshot(
            session, household_id, None, service.month_bounds(month)[1]
        )
        return DashboardScoreOut(
            status="ready",
            score=None if held is None else held.score,
            formula_version=None if held is None else held.formula_version,
            scored_on=None if held is None else held.scored_on,
            previous_score=await _previous_score(
                session,
                household_id,
                month,
                None if held is None else held.formula_version,
            ),
        )

    current = await scores.current_score(session, household_id, currency, today)
    result = current.result
    notice = (
        None
        if current.missing_month is None
        else NoticeOut(
            code="last_month_missing",
            month=current.missing_month,
            message=missing_line(current.missing_month, current.held_from),
        )
    )
    scored = result is not None and result.score is not None
    # The month before the score shown, not before the month asked for: a
    # score held from earlier in last month would otherwise be compared with
    # itself, and the app would show no change.
    shown_from = current.held_from or today
    return DashboardScoreOut(
        status="ready",
        score=result.score if scored else None,
        formula_version=result.formula_version if scored else None,
        scored_on=shown_from if scored else None,
        previous_score=await _previous_score(
            session,
            household_id,
            shown_from.replace(day=1),
            result.formula_version if scored else None,
        ),
        notice=notice,
    )


async def _previous_score(
    session: AsyncSession,
    household_id: uuid.UUID,
    month: date,
    formula_version: str | None,
) -> int | None:
    """The last snapshot in the month before [month], for "+4 since last month".

    None when there is none, when no score is shown, and when that snapshot was
    scored by a different formula (backend #66): the day v2 ships, a change
    from a v1 score would be partly the formula moving, not the person. The
    app shows no change label for None.
    """
    if formula_version is None:
        return None
    before = service.months_back(month, 2)[0]
    snapshot = await _last_snapshot(
        session, household_id, before, service.month_bounds(before)[1]
    )
    if snapshot is None or snapshot.formula_version != formula_version:
        return None
    return snapshot.score


async def _budget_section(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    month: date,
    today: date,
    learning: LearningState,
    money,
) -> DashboardBudgetOut:
    view = await budgets.budget_for(
        session, household_id, currency, month, today, ready=learning.ready
    )
    return _budget_out(view, learning, money)


async def _isolated(session: AsyncSession, section: str, household_id, work):
    """Run one optional section in a savepoint; on failure, None and a log line.

    The budget and the score are additions to Home. Before them, nothing
    outside the month's own figures could take Home down, and a fault in
    either — or a lock held too long — must not start to: the section is left
    out (the field is null, which every client already handles) and the rest
    of the dashboard is served. The savepoint discards anything the section
    half-wrote, so the commit below never carries it. The log names the
    section and the household id only.
    """
    try:
        async with session.begin_nested():
            return await work
    except Exception:
        log.exception(
            "dashboard_section_failed", section=section, household_id=str(household_id)
        )
        return None


@router.get("/dashboard", response_model=DashboardOut)
async def read_dashboard(
    month: str | None = Query(default=None, description="YYYY-MM; defaults to now."),
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> DashboardOut:
    """One month: what happened, against what was expected.

    A month with nothing in it is a 200 with zeroes and an empty trend, not a
    404. "You have no transactions yet" is a state of the account, and a client
    should not have to treat an empty first month as an error.
    """
    household = identity.household
    currency = await currency_for(session, household)
    today = _today()
    when = _month_or_now(month)
    built = await service.build(session, household.id, currency, when, today=today)
    spend = await service.spend_by_category(session, household.id, currency, when)
    fresh = await service.as_of(session, household.id, currency)
    learning = await learning_state(session, household.id, currency, today)

    def money(minor: int) -> str:
        return from_minor_units(minor, currency)

    # Budget and score only for a household that has them, and never for a
    # month that has not begun: browsing ahead on Home must not create a
    # budget. Everything else on the dashboard is unaffected either way.
    features = (await resolve(session, household)).features

    def has(key: str) -> bool:
        feature = features.get(key)
        return feature is not None and feature.enabled

    current_month = today.replace(day=1)
    budget = None
    if has(FEATURE_BUDGET) and when <= current_month:
        budget = await _isolated(
            session,
            "budget",
            household.id,
            _budget_section(
                session, household.id, currency, when, today, learning, money
            ),
        )
    score = None
    if has(FEATURE_SCORE) and when <= current_month:
        score = await _isolated(
            session,
            "health_score",
            household.id,
            _score_out(session, household.id, currency, when, today, learning),
        )
    # Reading the current month keeps today's score snapshot and settles the
    # month's budget, exactly as their own screens would.
    await session.commit()

    def maybe(minor: int | None) -> str | None:
        return None if minor is None else money(minor)

    return DashboardOut(
        month=built.month,
        currency=currency,
        net=money(built.net_minor_units),
        previous_net=maybe(built.previous_net_minor_units),
        income=FlowOut(
            actual=money(built.income.actual_minor_units),
            expected=maybe(built.income.expected_minor_units),
            previous=maybe(built.income.previous_minor_units),
        ),
        expenses=FlowOut(
            actual=money(built.expenses.actual_minor_units),
            expected=maybe(built.expenses.expected_minor_units),
            previous=maybe(built.expenses.previous_minor_units),
        ),
        investments=StockOut(
            balance=money(built.investments.balance_minor_units),
            moved=money(built.investments.moved_minor_units),
            previous_moved=maybe(built.investments.previous_moved_minor_units),
            withdrawn=money(built.investments.withdrawn_minor_units),
        ),
        debts=StockOut(
            balance=money(built.debts.balance_minor_units),
            moved=money(built.debts.moved_minor_units),
            previous_moved=maybe(built.debts.previous_moved_minor_units),
            withdrawn=money(built.debts.withdrawn_minor_units),
        ),
        commitments=[
            CommitmentOut(
                name=item.name,
                expected=money(item.expected_minor_units),
                match=(
                    MatchOut(
                        id=item.match.transaction_id,
                        occurred_on=item.match.occurred_on,
                        amount=money(item.match.amount_minor_units),
                        description=item.match.description,
                    )
                    if item.match is not None
                    else None
                ),
                due_day=item.due_day,
            )
            for item in built.commitments
        ],
        trend=[
            MonthPointOut(
                month=point.month,
                net=maybe(point.net_minor_units),
                income=maybe(point.income_minor_units),
                expenses=maybe(point.expenses_minor_units),
                invested=maybe(point.invested_minor_units),
                withdrawn=maybe(point.withdrawn_minor_units),
                debt_paid=maybe(point.debt_paid_minor_units),
            )
            for point in built.trend
        ],
        daily=[
            DayPointOut(day=point.day, net=money(point.net_minor_units))
            for point in built.daily
        ],
        pending_review=built.pending_review,
        spend_by_category=[
            CategorySpendOut(
                category_id=entry.category_id,
                slug=entry.slug,
                name=entry.name,
                spent=money(entry.spent_minor_units),
            )
            for entry in spend
        ],
        budget=budget,
        health_score=score,
        as_of=AsOfOut(
            latest_transaction_on=fresh.latest_transaction_on,
            last_import_at=fresh.last_import_at,
        ),
        learning=_learning_out(learning),
    )
