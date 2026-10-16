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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.categorization import Category
from app.models.enums import AccountKind, BalanceSource, TransactionDirection
from app.models.money import Account, StatementImport, Transaction
from app.models.planning import Debt
from app.services.dashboard import (
    DEBT_PAYMENT_SLUG,
    TRANSFERS_SLUG,
    countable,
    month_bounds,
)

__all__ = [
    "DEBT_ACCOUNT_KINDS",
    "covered_minimums",
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


async def covered_minimums(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    month: date,
    debt_paid: int,
) -> int:
    """How much of the debts' minimums [month]'s payments cover (score v4).

    A debt linked to an account whose statement for [month] is imported is
    measured on its own: the payments that reached that account (its
    transfers and debt payments in), up to its minimum. Every other debt
    shares what is left of the month's debt paid — as all of them did before —
    up to their minimums together. So a card paid off in full counts toward
    the card, and can no longer hide a missed loan payment, once the card is
    linked and its statement is in.

    With nothing linked this is `min(debt_paid, minimums)`: the v3 measure.
    """
    first, last = month_bounds(month)
    in_month = (Transaction.occurred_on >= first, Transaction.occurred_on <= last)
    debts = list(
        (
            await session.execute(
                select(Debt).where(
                    Debt.household_id == household_id,
                    Debt.currency == currency,
                    Debt.minimum_payment_minor_units.is_not(None),
                )
            )
        ).scalars()
    )
    linked = {d.account_id for d in debts if d.account_id is not None}
    with_statement: set[uuid.UUID] = set()
    if linked:
        result = await session.execute(
            select(Transaction.account_id)
            .where(
                *countable(household_id, currency),
                *in_month,
                Transaction.account_id.in_(linked),
            )
            .distinct()
        )
        with_statement = set(result.scalars().all())

    received: dict[uuid.UUID, int] = {}
    card_credits = 0
    paired_debits = 0
    if with_statement:
        result = await session.execute(
            select(
                Transaction.account_id,
                Account.kind,
                func.sum(Transaction.amount_minor_units),
            )
            .join(Account, Account.id == Transaction.account_id)
            .join(Category, Category.id == Transaction.category_id)
            .where(
                *countable(household_id, currency),
                *in_month,
                Transaction.account_id.in_(with_statement),
                Transaction.direction == TransactionDirection.credit,
                Category.slug.in_((TRANSFERS_SLUG, DEBT_PAYMENT_SLUG)),
            )
            .group_by(Transaction.account_id, Account.kind)
        )
        for account_id, kind, total in result:
            received[account_id] = int(total)
            if kind == AccountKind.credit_card and total:
                # Counted in debt paid already (`dashboard.paid_card_debt`):
                # attributed here, so it leaves the shared pool.
                card_credits += int(total)
        # A bank-side debt payment paired with one of those accounts — a loan's
        # — is the same money arriving there: also attributed, not pooled.
        partner = aliased(Transaction)
        paired_debits = int(
            await session.scalar(
                select(func.coalesce(func.sum(Transaction.amount_minor_units), 0))
                .join(partner, partner.id == Transaction.transfer_pair_id)
                .join(Category, Category.id == Transaction.category_id)
                .where(
                    *countable(household_id, currency),
                    *in_month,
                    Transaction.direction == TransactionDirection.debit,
                    Category.slug == DEBT_PAYMENT_SLUG,
                    partner.account_id.in_(with_statement),
                )
            )
            or 0
        )

    covered = 0
    pooled_required = 0
    for debt in debts:
        minimum = debt.minimum_payment_minor_units or 0
        if debt.account_id in with_statement:
            covered += min(received.get(debt.account_id, 0), minimum)
        else:
            pooled_required += minimum
    pool = max(0, debt_paid - card_credits - paired_debits)
    return covered + min(pool, pooled_required)
