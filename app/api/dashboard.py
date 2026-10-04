"""`GET /dashboard` — the monthly overview both apps render.

Computed on the server, not on each platform, for the reason shared logic
exists at all: Android and iOS must not be able to disagree about a number
somebody is making a decision from. A figure derived twice is derived
differently eventually.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.core.money import from_minor_units
from app.db import get_session
from app.schemas.dashboard import (
    CommitmentOut,
    DashboardOut,
    DayPointOut,
    FlowOut,
    MatchOut,
    MonthPointOut,
    StockOut,
)
from app.services import dashboard as service
from app.services.capabilities import currency_for
from app.services.identity import ResolvedIdentity

router = APIRouter(tags=["dashboard"])

_MONTH = re.compile(r"^(\d{4})-(\d{2})$")


def _parse_month(raw: str | None) -> date:
    """`YYYY-MM`, or the current UTC month.

    UTC rather than the household's own timezone, which we do not store.
    Somebody opening the app late on the 31st sees the next month a few hours
    early — the error direction to prefer, since the alternative is showing a
    month that has already ended as if it were still running.
    """
    if raw is None:
        now = datetime.now(UTC)
        return date(now.year, now.month, 1)

    found = _MONTH.match(raw)
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_month", "message": "Expected YYYY-MM."},
        )
    year, month = int(found.group(1)), int(found.group(2))
    if not 1 <= month <= 12 or not 1970 <= year <= 2999:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_month", "message": "Expected YYYY-MM."},
        )
    return date(year, month, 1)


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
    currency = await currency_for(session, identity.household)
    built = await service.build(
        session, identity.household.id, currency, _parse_month(month)
    )

    def money(minor: int) -> str:
        return from_minor_units(minor, currency)

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
        ),
        expenses=FlowOut(
            actual=money(built.expenses.actual_minor_units),
            expected=maybe(built.expenses.expected_minor_units),
        ),
        investments=StockOut(
            balance=money(built.investments.balance_minor_units),
            moved=money(built.investments.moved_minor_units),
        ),
        debts=StockOut(
            balance=money(built.debts.balance_minor_units),
            moved=money(built.debts.moved_minor_units),
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
            )
            for item in built.commitments
        ],
        trend=[
            MonthPointOut(month=point.month, net=maybe(point.net_minor_units))
            for point in built.trend
        ],
        daily=[
            DayPointOut(day=point.day, net=money(point.net_minor_units))
            for point in built.daily
        ],
        pending_review=built.pending_review,
    )
