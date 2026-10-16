"""A household's debts outside the setup wizard (#76).

The wizard (`financial_setup`) asks five things about a debt. The payoff plan
needs more — what it is, a promotional rate and its end, which account it is,
and a balance kept current — so debts are edited here too. The two share rows:
the wizard updates its own debts in place by position and leaves everything
it does not ask about alone.

A balance carries its date and source. Changing it here is the person's own
figure, as of today; a statement for the linked account replaces it only when
that statement closed later (`take_statement_balance`).
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AccountKind, BalanceSource
from app.models.money import Account, StatementImport
from app.models.planning import Debt

__all__ = [
    "DEBT_ACCOUNT_KINDS",
    "STALE_AFTER_DAYS",
    "UnusableAccount",
    "is_stale",
    "owned",
    "owned_account",
    "take_statement_balance",
]

log = structlog.get_logger()

# A balance not updated for this long, and not read from a statement, earns a
# reminder. A card's statement arrives monthly; half a month's grace on top.
STALE_AFTER_DAYS = 45

# The accounts a debt can be: money owed on them, with a statement of its own.
DEBT_ACCOUNT_KINDS = frozenset({AccountKind.credit_card, AccountKind.loan})


class UnusableAccount(Exception):
    """An account that is not this household's, or not a card or loan."""


def is_stale(debt: Debt, today: date) -> bool:
    if debt.balance_source == BalanceSource.statement:
        return False
    return debt.balance_as_of is None or today - debt.balance_as_of > timedelta(
        days=STALE_AFTER_DAYS
    )


async def owned(
    session: AsyncSession, household_id: uuid.UUID, debt_id: uuid.UUID
) -> Debt | None:
    """This household's debt, or None — the same for one that is missing and
    one that is someone else's."""
    return await session.scalar(
        select(Debt).where(Debt.id == debt_id, Debt.household_id == household_id)
    )


async def owned_account(
    session: AsyncSession, household_id: uuid.UUID, account_id: uuid.UUID
) -> Account:
    """The household's own credit-card or loan account, or `UnusableAccount`.

    A chequing account is not a debt: linking one would make its statement's
    closing balance — money the person has — read as money they owe.
    """
    account = await session.scalar(
        select(Account).where(
            Account.id == account_id, Account.household_id == household_id
        )
    )
    if account is None or account.kind not in DEBT_ACCOUNT_KINDS:
        raise UnusableAccount()
    return account


async def take_statement_balance(
    session: AsyncSession,
    household_id: uuid.UUID,
    account_id: uuid.UUID,
    record: StatementImport,
) -> int:
    """Give each debt linked to [account_id] the balance [record] closed at.

    Only when the statement closed **after** the balance the debt has: an old
    statement imported late must never overwrite a newer figure, whether that
    figure was typed or read. Nothing happens without a closing balance, a
    closing date, or a currency matching the debt's. Returns how many debts
    were updated.
    """
    if (
        record.closing_balance_minor_units is None
        or record.closing_date is None
        or record.currency is None
    ):
        return 0
    debts = (
        await session.execute(
            select(Debt).where(
                Debt.household_id == household_id,
                Debt.account_id == account_id,
                Debt.currency == record.currency,
            )
        )
    ).scalars()
    updated = 0
    for debt in debts:
        if debt.balance_as_of is not None and debt.balance_as_of >= record.closing_date:
            continue
        debt.balance_minor_units = record.closing_balance_minor_units
        debt.balance_as_of = record.closing_date
        debt.balance_source = BalanceSource.statement
        updated += 1
    if updated:
        # Counts and ids only: the balance is the household's.
        log.info(
            "debt_balance_from_statement",
            household_id=str(household_id),
            statement_import_id=str(record.id),
            debts=updated,
        )
    return updated
