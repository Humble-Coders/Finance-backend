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

**There is no status.** Income and monthly expense are mandatory and gated by
the onboarding rule (app/services/onboarding.py), which reads those columns
directly. The rest is optional, and for it a skipped answer and an unasked one
are the same fact — no row — so absence is the record and nothing here tracks
it (#29).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import MoneyError, from_minor_units, to_minor_units
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
]

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


async def _lock(session: AsyncSession, household: Household) -> None:
    """Serialise writes for one household.

    A save deletes the wizard's rows and inserts new ones. Two overlapping saves
    — a double tap, or a retry after a slow response — would otherwise each
    delete what they could see and then insert, leaving both sets behind; and on
    a household's first save both would insert a `financial_profile`, making the
    loser a 500 on the unique constraint. Locking the household row first makes
    them queue instead.
    """
    await session.execute(
        select(Household.id).where(Household.id == household.id).with_for_update()
    )


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
                .order_by(Debt.position, Debt.created_at, Debt.name)
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
                .order_by(Investment.position, Investment.created_at, Investment.name)
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
                .order_by(Obligation.position, Obligation.created_at, Obligation.name)
            )
        )
        .scalars()
        .all()
    )

    stored_currency = profile.currency if profile else currency
    income_units = profile.monthly_income_minor_units if profile else None
    expense_units = profile.monthly_expense_minor_units if profile else None
    return FinancialSetupOut(
        currency=stored_currency,
        income=(
            from_minor_units(income_units, stored_currency)
            if income_units is not None
            else None
        ),
        monthly_expense=(
            from_minor_units(expense_units, stored_currency)
            if expense_units is not None
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
    await _lock(session, household)

    income = (
        _amount(body.income, currency, "income")
        if body.income is not None and str(body.income).strip()
        else None
    )
    monthly_expense = (
        _amount(body.monthly_expense, currency, "monthly_expense")
        if body.monthly_expense is not None and str(body.monthly_expense).strip()
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
            position=i,
        )
        for i, d in enumerate(body.debts)
    ]
    investments = [
        Investment(
            household_id=household.id,
            name=v.name,
            amount_minor_units=_amount(v.amount, currency, f"investments.{i}.amount"),
            currency=currency,
            position=i,
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
            position=i,
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
    profile.monthly_expense_minor_units = monthly_expense
    profile.currency = currency

    await session.commit()
    return await _payload(session, household, currency)
