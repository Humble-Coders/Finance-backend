"""Money moving between a household's own accounts (#73).

A card bill paid from chequing appears twice: a debit on the bank statement and
a "payment received" credit on the card's. Money put into savings is a debit
on one account and a deposit on another. Neither is money earned or spent, and
the dashboard counts neither once they are filed as transfers
(`dashboard.NOT_A_FLOW`). This module does the filing, after the categorizer
has had its say:

* **Pairs.** A row and its opposite on *another* of the household's accounts —
  same amount and currency, within `WINDOW_DAYS` — are linked, and the side
  that is not already a transfer becomes one. Run on every save against rows
  already saved, so it works whichever statement is imported first.
* **A card being paid.** A credit on a credit-card account that the
  categorizer filed as income, a transfer or "other" is the card being paid,
  partner or not: its purchases are on the same statement, so counting it as
  income is always wrong. A refund comes back as its shop's category and is
  left alone — told apart by what it is, not by matching words like "PAYMENT"
  in a language list.

A bank-side card payment with **no** partner is left counted. Until the card's
statement is imported it is the only record of that spending, and filing it
away would understate the month; it is paired when the statement arrives.

Every row this files goes to review as `own_transfer`, so a person confirms
it. A wrong pair hides real money, so the rules lean towards not pairing:

* one partner each (`transfer_pair_id`), the nearest date wins, and a tie
  pairs nothing;
* a row whose amount is in doubt or that may be a copy is never paired;
* a category the person chose is never changed — a row typed in by hand, or a
  merchant they have a correction rule for. Such a row may still be the half
  of a pair that needs no change.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

import structlog
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.categorization import Category, CategoryCorrection
from app.models.enums import (
    AccountKind,
    ReviewReason,
    TransactionDirection,
    TransactionSource,
)
from app.models.money import Account, Transaction
from app.services.corrections import merchant_key
from app.services.dashboard import DEBT_PAYMENT_SLUG, SAVINGS_SLUG, TRANSFERS_SLUG

__all__ = ["WINDOW_DAYS", "Paired", "pair_transfers"]

log = structlog.get_logger()

# Card payments post a few business days after they leave the bank; a week-end
# and a holiday is five days.
WINDOW_DAYS = 5

INCOME_SLUG = "income"
OTHER_SLUG = "other"

# What a row may be re-filed *from*. Anything else — groceries, rent — is the
# categorizer saying it is spending, and a coincidence of amounts is not enough
# to overrule it. No category at all is re-fileable: the row is waiting for one.
_REFILEABLE = frozenset({INCOME_SLUG, OTHER_SLUG, DEBT_PAYMENT_SLUG})

# Accounts money arrives at only from another of the household's own: a card
# being paid, a loan being paid down, savings being put aside.
_RECEIVING = frozenset(
    {
        AccountKind.credit_card,
        AccountKind.loan,
        AccountKind.savings,
        AccountKind.investment,
    }
)
# Accounts money leaves mostly for another of the household's own.
_SENDING = frozenset({AccountKind.savings, AccountKind.investment})

# A card credit filed as one of these is the card being paid.
_CARD_PAYMENT = frozenset({INCOME_SLUG, TRANSFERS_SLUG, OTHER_SLUG})

# A row flagged for these is not evidence of anything yet.
_DOUBTFUL = (ReviewReason.low_confidence, ReviewReason.suspected_duplicate)


@dataclass(frozen=True)
class Paired:
    pairs: int = 0
    # Rows whose category this changed; each is now waiting for review.
    refiled: int = 0


@dataclass
class _Side:
    row: Transaction
    kind: AccountKind
    slug: str | None
    protected: bool = False


def _keeps(side: _Side, partner: _Side) -> bool:
    """Whether [side] stays as it is when paired with [partner].

    Savings stays savings: it is what the investments card counts as set
    aside, and it is already not a flow. A loan payment stays a debt payment:
    the loan's statement lists no purchases, so the payment *is* the debt being
    paid — unlike a card's, whose purchases are the spending.
    """
    if side.slug in (TRANSFERS_SLUG, SAVINGS_SLUG):
        return True
    return (
        side.slug == DEBT_PAYMENT_SLUG
        and side.row.direction == TransactionDirection.debit
        and partner.kind == AccountKind.loan
    )


def _plan(new: _Side, other: _Side) -> list[_Side] | None:
    """The rows to re-file if these two are a pair, or None if they are not."""
    debit, credit = (
        (new, other)
        if new.row.direction == TransactionDirection.debit
        else (other, new)
    )
    evidence = (
        credit.kind in _RECEIVING
        or debit.kind in _SENDING
        or bool({debit.slug, credit.slug} & {TRANSFERS_SLUG, SAVINGS_SLUG})
    )
    if not evidence:
        return None
    changes = []
    for side, partner in ((debit, credit), (credit, debit)):
        if _keeps(side, partner):
            continue
        if side.protected or (side.slug is not None and side.slug not in _REFILEABLE):
            return None
        changes.append(side)
    return changes


def _not_doubtful(row) -> object:
    return or_(
        row.needs_review.is_(False),
        row.review_reason.is_(None),
        row.review_reason.not_in(_DOUBTFUL),
    )


async def pair_transfers(
    session: AsyncSession,
    household_id: uuid.UUID,
    saved_ids: list[uuid.UUID],
    *,
    chosen: bool = False,
) -> Paired:
    """File the moves between own accounts among [saved_ids]; see the module.

    Call after `file_rows`, so the categorizer cannot overwrite what this
    files. `chosen` is whether the person picked the saved rows' category
    themselves — a transaction typed in with one; an import never has.

    A fixed number of queries whatever the row count — the candidates for
    every saved row come back from one.
    """
    if not saved_ids:
        return Paired()
    await session.flush()

    result = await session.execute(
        select(Transaction, Account.kind, Category.slug)
        .join(Account, Account.id == Transaction.account_id)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(
            Transaction.household_id == household_id,
            Transaction.id.in_(saved_ids),
            Transaction.transfer_pair_id.is_(None),
            _not_doubtful(Transaction),
        )
    )
    new = {row.id: _Side(row, kind, slug, chosen) for row, kind, slug in result}
    if not new:
        return Paired()

    saved = aliased(Transaction)
    window = timedelta(days=WINDOW_DAYS)
    result = await session.execute(
        select(Transaction, Account.kind, Category.slug, saved.id)
        .join(
            saved,
            and_(
                saved.id.in_(list(new)),
                saved.account_id != Transaction.account_id,
                saved.direction != Transaction.direction,
                saved.amount_minor_units == Transaction.amount_minor_units,
                saved.currency == Transaction.currency,
                Transaction.occurred_on >= saved.occurred_on - window,
                Transaction.occurred_on <= saved.occurred_on + window,
            ),
        )
        .join(Account, Account.id == Transaction.account_id)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(
            Transaction.household_id == household_id,
            Transaction.id.not_in(saved_ids),
            Transaction.transfer_pair_id.is_(None),
            _not_doubtful(Transaction),
        )
    )
    others: dict[uuid.UUID, _Side] = {}
    edges: list[tuple[uuid.UUID, uuid.UUID]] = []
    for row, kind, slug, saved_id in result:
        others.setdefault(row.id, _Side(row, kind, slug))
        edges.append((saved_id, row.id))

    await _protect(session, household_id, list(new.values()), list(others.values()))

    # The nearest qualifying partner of each saved row; a tie is no answer.
    best: dict[uuid.UUID, tuple[int, uuid.UUID, list[_Side]]] = {}
    tied: set[uuid.UUID] = set()
    for saved_id, other_id in edges:
        changes = _plan(new[saved_id], others[other_id])
        if changes is None:
            continue
        gap = abs(
            (new[saved_id].row.occurred_on - others[other_id].row.occurred_on).days
        )
        current = best.get(saved_id)
        if current is None or gap < current[0]:
            best[saved_id] = (gap, other_id, changes)
            tied.discard(saved_id)
        elif gap == current[0]:
            tied.add(saved_id)
    for saved_id in tied:
        best.pop(saved_id, None)

    # And one saved row per partner: two rows wanting the same one — two
    # $500 payments to one $500 bill — are both left for a person.
    claims: dict[uuid.UUID, list[uuid.UUID]] = {}
    for saved_id, (_, other_id, _) in best.items():
        claims.setdefault(other_id, []).append(saved_id)
    for other_id, claimants in claims.items():
        if len(claimants) > 1:
            gaps = sorted(best[c][0] for c in claimants)
            nearest = [c for c in claimants if best[c][0] == gaps[0]]
            for c in claimants:
                if len(nearest) > 1 or c != nearest[0]:
                    best.pop(c)

    to_file: list[_Side] = []
    for saved_id, (_, other_id, changes) in best.items():
        new[saved_id].row.transfer_pair_id = other_id
        others[other_id].row.transfer_pair_id = saved_id
        to_file.extend(changes)

    # A card being paid, partner or not.
    for saved_id, side in new.items():
        if (
            saved_id not in best
            and side.kind == AccountKind.credit_card
            and side.row.direction == TransactionDirection.credit
            and side.slug in _CARD_PAYMENT - {TRANSFERS_SLUG}
            and not side.protected
        ):
            to_file.append(side)

    if to_file:
        transfers_id = await session.scalar(
            select(Category.id).where(
                Category.household_id.is_(None), Category.slug == TRANSFERS_SLUG
            )
        )
        for side in to_file:
            side.row.category_id = transfers_id
            side.row.needs_review = True
            side.row.review_reason = ReviewReason.own_transfer
    await session.flush()

    outcome = Paired(pairs=len(best), refiled=len(to_file))
    if outcome.pairs or outcome.refiled:
        # Counts only: which rows, and for how much, is the household's.
        log.info(
            "transfers_filed",
            household_id=str(household_id),
            pairs=outcome.pairs,
            refiled=outcome.refiled,
        )
    return outcome


async def _protect(
    session: AsyncSession,
    household_id: uuid.UUID,
    new: list[_Side],
    others: list[_Side],
) -> None:
    """Mark the rows whose category a person chose.

    A merchant they have a correction rule for is one they have already told
    us about — `learn` records one whenever a category is changed. Matched
    with `merchant_key`, the only definition of a merchant's form, in Python
    rather than a second version written in SQL.

    An earlier row typed in by hand is treated as chosen if it has a category:
    nothing records whether the person picked it or the categorizer did, and
    of the two mistakes, leaving a hand-typed row alone is the cheap one. The
    rows just saved say for themselves (`chosen`).
    """
    for side in others:
        if side.row.source == TransactionSource.manual and side.row.category_id:
            side.protected = True
    sides = [*new, *others]
    keys = {key for side in sides if (key := merchant_key(side.row.merchant))}
    if not keys:
        return
    result = await session.execute(
        select(CategoryCorrection.merchant_pattern).where(
            CategoryCorrection.household_id == household_id,
            CategoryCorrection.merchant_pattern.in_(keys),
        )
    )
    ruled = set(result.scalars().all())
    for side in sides:
        if merchant_key(side.row.merchant) in ruled:
            side.protected = True
