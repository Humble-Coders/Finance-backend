"""`/debts` — the household's debts, kept current for the payoff plan (#76).

Not feature-gated: debts are the setup wizard's data, which every onboarded
household has. Another household's debt or account is `404`, never `403`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.core.money import MoneyError, from_minor_units, to_minor_units
from app.db import get_session
from app.models.enums import BalanceSource
from app.models.planning import Debt
from app.schemas.debts import DebtIn, DebtOut, DebtPatch, DebtsOut
from app.services import debts as service
from app.services.capabilities import currency_for
from app.services.financial_setup import SetupValidationError, rate_bps, rate_percent
from app.services.identity import ResolvedIdentity

router = APIRouter(prefix="/debts", tags=["debts"])


def _today() -> date:
    """UTC, as the dashboard and goals count days."""
    return datetime.now(UTC).date()


def _unprocessable(code: str, field: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": code, "field": field, "message": message},
    )


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"}
    )


def _amount(raw: str, currency: str, field: str) -> int:
    """A non-negative decimal string as minor units, or `422 invalid_amount`."""
    try:
        value = to_minor_units(raw, currency)
    except MoneyError as error:
        raise _unprocessable("invalid_amount", field, str(error)) from error
    if value < 0:
        raise _unprocessable("invalid_amount", field, "must not be negative")
    return value


def _rate(raw: str | None, field: str) -> int | None:
    try:
        return rate_bps(raw, field)
    except SetupValidationError as error:
        raise _unprocessable("invalid_rate", error.field, error.message) from error


def _promotion(rate: int | None, ends_on: date | None) -> None:
    if (rate is None) != (ends_on is None):
        raise _unprocessable(
            "invalid_promotion",
            "promo_ends_on" if rate is not None else "promo_rate_percent",
            "A promotional rate needs the day it ends, and the day needs the rate.",
        )


async def _account(
    session: AsyncSession, household_id: uuid.UUID, account_id: uuid.UUID | None
) -> uuid.UUID | None:
    if account_id is None:
        return None
    try:
        return (await service.owned_account(session, household_id, account_id)).id
    except service.UnusableAccount as error:
        # 404 for an account that is someone else's — and for one that is not a
        # card or loan, with a reason, since the id itself is the caller's own.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "unusable_account",
                "message": "Link a debt to one of your credit-card or loan accounts.",
            },
        ) from error


def _out(debt: Debt, today: date) -> DebtOut:
    currency = debt.currency

    def money(minor: int | None) -> str | None:
        return None if minor is None else from_minor_units(minor, currency)

    return DebtOut(
        id=debt.id,
        name=debt.name,
        kind=debt.kind,
        balance=from_minor_units(debt.balance_minor_units, currency),
        minimum_payment=money(debt.minimum_payment_minor_units),
        interest_rate_percent=rate_percent(debt.interest_rate_bps),
        promo_rate_percent=rate_percent(debt.promo_rate_bps),
        promo_ends_on=debt.promo_ends_on,
        due_day=debt.due_day,
        account_id=debt.account_id,
        balance_as_of=debt.balance_as_of,
        balance_source=debt.balance_source,
        balance_stale=service.is_stale(debt, today),
        from_setup=debt.entered_via_setup,
    )


@router.get("", response_model=DebtsOut)
async def list_debts(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> DebtsOut:
    """Every debt, the wizard's first in its order, then the rest by age."""
    household = identity.household
    today = _today()
    rows = (
        await session.execute(
            select(Debt)
            .where(Debt.household_id == household.id)
            .order_by(
                Debt.entered_via_setup.desc(),
                Debt.position,
                Debt.created_at,
                Debt.name,
            )
        )
    ).scalars()
    return DebtsOut(
        currency=await currency_for(session, household),
        debts=[_out(debt, today) for debt in rows],
    )


@router.post("", response_model=DebtOut, status_code=status.HTTP_201_CREATED)
async def add_debt(
    body: DebtIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> DebtOut:
    """A debt added from the Debts screen. Not the wizard's: a later wizard
    save never touches it."""
    household = identity.household
    today = _today()
    currency = await currency_for(session, household)
    promo = _rate(body.promo_rate_percent, "promo_rate_percent")
    _promotion(promo, body.promo_ends_on)
    debt = Debt(
        household_id=household.id,
        name=body.name.strip(),
        kind=body.kind,
        balance_minor_units=_amount(body.balance, currency, "balance"),
        minimum_payment_minor_units=(
            None
            if body.minimum_payment in (None, "")
            else _amount(body.minimum_payment, currency, "minimum_payment")
        ),
        interest_rate_bps=_rate(body.interest_rate_percent, "interest_rate_percent"),
        promo_rate_bps=promo,
        promo_ends_on=body.promo_ends_on,
        due_day=body.due_day,
        account_id=await _account(session, household.id, body.account_id),
        currency=currency,
        entered_via_setup=False,
        balance_as_of=today,
        balance_source=BalanceSource.manual,
    )
    session.add(debt)
    await session.flush()
    await session.commit()
    return _out(debt, today)


@router.patch("/{debt_id}", response_model=DebtOut)
async def edit_debt(
    debt_id: uuid.UUID,
    body: DebtPatch,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> DebtOut:
    """Change the fields sent; null clears those that may be empty.

    A new balance is the person's own figure, as of today ("Update balance");
    a balance sent unchanged keeps its date and source.
    """
    household = identity.household
    today = _today()
    debt = await service.owned(session, household.id, debt_id)
    if debt is None:
        raise _not_found()
    sent = body.model_fields_set
    for required in ("name", "kind", "balance"):
        if required in sent and getattr(body, required) is None:
            raise _unprocessable("invalid_value", required, "This can't be cleared.")

    if "name" in sent:
        debt.name = body.name.strip()
    if "kind" in sent:
        debt.kind = body.kind
    if "balance" in sent:
        balance = _amount(body.balance, debt.currency, "balance")
        if balance != debt.balance_minor_units:
            debt.balance_minor_units = balance
            debt.balance_as_of = today
            debt.balance_source = BalanceSource.manual
    if "minimum_payment" in sent:
        debt.minimum_payment_minor_units = (
            None
            if body.minimum_payment in (None, "")
            else _amount(body.minimum_payment, debt.currency, "minimum_payment")
        )
    if "interest_rate_percent" in sent:
        debt.interest_rate_bps = _rate(
            body.interest_rate_percent, "interest_rate_percent"
        )
    promo = debt.promo_rate_bps
    ends_on = debt.promo_ends_on
    if "promo_rate_percent" in sent:
        promo = _rate(body.promo_rate_percent, "promo_rate_percent")
    if "promo_ends_on" in sent:
        ends_on = body.promo_ends_on
    _promotion(promo, ends_on)
    debt.promo_rate_bps, debt.promo_ends_on = promo, ends_on
    if "due_day" in sent:
        debt.due_day = body.due_day
    if "account_id" in sent:
        debt.account_id = await _account(session, household.id, body.account_id)

    await session.flush()
    await session.commit()
    return _out(debt, today)


@router.delete("/{debt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_debt(
    debt_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> Response:
    debt = await service.owned(session, identity.household.id, debt_id)
    if debt is None:
        raise _not_found()
    await session.delete(debt)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
