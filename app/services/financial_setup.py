"""Reading and writing the financial setup wizard (PRD F1).

Two rules shape everything here.

**Money crosses the boundary exactly once.** Amounts arrive as decimal strings,
are converted by `app.core.money` alone, and are stored as integer minor units
(PRD §4.4). Nothing here multiplies or divides an amount.

**A save replaces what the wizard owns, and only that.** The client sends the
whole wizard after every step, which is what makes it resumable: the same call
repeated leaves the same rows. Debts are the exception that needs care —
statements create debts too (M3), so only rows flagged `entered_via_setup` are
replaced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import MoneyError, exponent_for, from_minor_units, to_minor_units
from app.models.identity import Household
from app.models.planning import Debt
from app.models.setup import FinancialProfile, Investment, Obligation
from app.schemas.financial_setup import (
    DebtOut,
    FinancialSetupIn,
    FinancialSetupOut,
    InvestmentOut,
    ObligationOut,
)
from app.services.capabilities import currency_for

__all__ = [
    "SetupValidationError",
    "get_setup",
    "save_setup",
    "skip_setup",
]

STATUS_NOT_STARTED = "not_started"
STATUS_SKIPPED = "skipped"
STATUS_COMPLETED = "completed"

# Basis points: 5.25% -> 525, an integer for the same reason money is.
_BPS_PER_PERCENT = 100
_MAX_RATE_PERCENT = Decimal(100)


@dataclass(frozen=True)
class SetupValidationError(Exception):
    """An amount the wizard cannot store, named by its path in the request.

    The path (`debts.0.balance`) is what lets the client highlight the row the
    user typed, rather than showing a whole-form error.
    """

    field: str
    message: str


def _amount(value: str, currency: str, field: str) -> int:
    """A non-negative decimal string -> minor units, or a named error."""
    try:
        minor_units = to_minor_units(value, currency)
    except MoneyError as exc:
        raise SetupValidationError(field, str(exc)) from exc
    if minor_units < 0:
        # Money itself allows negatives (refunds, debts); nothing the wizard
        # asks for can be one.
        raise SetupValidationError(field, "must not be negative")
    return minor_units


def _rate_bps(value: str | None, field: str) -> int | None:
    if value is None or not str(value).strip():
        return None
    try:
        rate = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise SetupValidationError(field, "not a percentage") from exc
    if not rate.is_finite() or rate < 0 or rate > _MAX_RATE_PERCENT:
        raise SetupValidationError(field, "must be between 0 and 100")
    scaled = rate * _BPS_PER_PERCENT
    if scaled != scaled.to_integral_value():
        raise SetupValidationError(field, "more than two decimal places")
    return int(scaled)


def _rate_percent(bps: int | None) -> str | None:
    if bps is None:
        return None
    return f"{Decimal(bps) / _BPS_PER_PERCENT:.2f}"


def _status(profile: FinancialProfile | None) -> str:
    if profile is None:
        return STATUS_NOT_STARTED
    if profile.setup_completed_at is not None:
        # Completed outranks skipped: finishing later is the stronger signal.
        return STATUS_COMPLETED
    if profile.setup_skipped_at is not None:
        return STATUS_SKIPPED
    return STATUS_NOT_STARTED


async def _profile(
    session: AsyncSession, household: Household
) -> FinancialProfile | None:
    result = await session.execute(
        select(FinancialProfile).where(FinancialProfile.household_id == household.id)
    )
    return result.scalar_one_or_none()


async def _payload(
    session: AsyncSession, household: Household, currency: str
) -> FinancialSetupOut:
    profile = await _profile(session, household)
    debts = (
        (
            await session.execute(
                select(Debt)
                .where(
                    Debt.household_id == household.id, Debt.entered_via_setup.is_(True)
                )
                .order_by(Debt.created_at, Debt.name)
            )
        )
        .scalars()
        .all()
    )
    investments = (
        (
            await session.execute(
                select(Investment)
                .where(Investment.household_id == household.id)
                .order_by(Investment.created_at, Investment.name)
            )
        )
        .scalars()
        .all()
    )
    obligations = (
        (
            await session.execute(
                select(Obligation)
                .where(Obligation.household_id == household.id)
                .order_by(Obligation.created_at, Obligation.name)
            )
        )
        .scalars()
        .all()
    )

    stored_currency = profile.currency if profile else currency
    return FinancialSetupOut(
        status=_status(profile),
        currency=stored_currency,
        income=(
            from_minor_units(profile.monthly_income_minor_units, stored_currency)
            if profile is not None and profile.monthly_income_minor_units is not None
            else None
        ),
        debts=[
            DebtOut(
                name=d.name,
                balance=from_minor_units(d.balance_minor_units, d.currency),
                minimum_payment=(
                    from_minor_units(d.minimum_payment_minor_units, d.currency)
                    if d.minimum_payment_minor_units is not None
                    else None
                ),
                interest_rate_percent=_rate_percent(d.interest_rate_bps),
            )
            for d in debts
        ],
        investments=[
            InvestmentOut(
                name=i.name, amount=from_minor_units(i.amount_minor_units, i.currency)
            )
            for i in investments
        ],
        obligations=[
            ObligationOut(
                name=o.name,
                monthly_amount=from_minor_units(
                    o.monthly_amount_minor_units, o.currency
                ),
            )
            for o in obligations
        ],
    )


async def get_setup(session: AsyncSession, household: Household) -> FinancialSetupOut:
    return await _payload(session, household, await currency_for(session, household))


async def save_setup(
    session: AsyncSession, household: Household, body: FinancialSetupIn
) -> FinancialSetupOut:
    """Replace the wizard's data in one transaction.

    Everything is converted **before** anything is deleted, so a bad amount in
    the last row cannot leave the household with half a wizard.
    """
    currency = await currency_for(session, household)
    exponent_for(currency)  # fails loudly on a currency we cannot denominate

    income = (
        _amount(body.income, currency, "income")
        if body.income is not None and str(body.income).strip()
        else None
    )
    debts = [
        Debt(
            household_id=household.id,
            name=d.name,
            balance_minor_units=_amount(d.balance, currency, f"debts.{i}.balance"),
            minimum_payment_minor_units=(
                _amount(d.minimum_payment, currency, f"debts.{i}.minimum_payment")
                if d.minimum_payment is not None and str(d.minimum_payment).strip()
                else None
            ),
            interest_rate_bps=_rate_bps(
                d.interest_rate_percent, f"debts.{i}.interest_rate_percent"
            ),
            currency=currency,
            entered_via_setup=True,
        )
        for i, d in enumerate(body.debts)
    ]
    investments = [
        Investment(
            household_id=household.id,
            name=v.name,
            amount_minor_units=_amount(v.amount, currency, f"investments.{i}.amount"),
            currency=currency,
        )
        for i, v in enumerate(body.investments)
    ]
    obligations = [
        Obligation(
            household_id=household.id,
            name=o.name,
            monthly_amount_minor_units=_amount(
                o.monthly_amount, currency, f"obligations.{i}.monthly_amount"
            ),
            currency=currency,
        )
        for i, o in enumerate(body.obligations)
    ]

    # Only what the wizard owns. Statement-derived debts (M3) are untouched.
    await session.execute(
        delete(Debt).where(
            Debt.household_id == household.id, Debt.entered_via_setup.is_(True)
        )
    )
    await session.execute(
        delete(Investment).where(Investment.household_id == household.id)
    )
    await session.execute(
        delete(Obligation).where(Obligation.household_id == household.id)
    )
    session.add_all([*debts, *investments, *obligations])

    profile = await _profile(session, household)
    if profile is None:
        profile = FinancialProfile(household_id=household.id, currency=currency)
        session.add(profile)
    profile.monthly_income_minor_units = income
    profile.currency = currency
    if body.finished and profile.setup_completed_at is None:
        profile.setup_completed_at = datetime.now(UTC)

    await session.commit()
    return await _payload(session, household, currency)


async def skip_setup(session: AsyncSession, household: Household) -> FinancialSetupOut:
    """Mark the wizard skipped, keeping anything already saved."""
    currency = await currency_for(session, household)
    profile = await _profile(session, household)
    if profile is None:
        profile = FinancialProfile(household_id=household.id, currency=currency)
        session.add(profile)
    if profile.setup_skipped_at is None:
        profile.setup_skipped_at = datetime.now(UTC)
    await session.commit()
    return await _payload(session, household, currency)
