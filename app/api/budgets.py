"""`/budgets` — a month's budget, generated from real spending (PRD F4).

Gated by `auto_budget`: the capabilities payload decides whether a client
shows budgets, and `require_feature` decides whether this answers at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.core.money import MoneyError, from_minor_units, to_minor_units
from app.db import get_session
from app.models.categorization import Category
from app.models.enums import TransactionDirection
from app.models.money import Account, Transaction
from app.models.setup import CommitmentPayment, Obligation
from app.schemas.budget import (
    AiLineOut,
    BudgetCommitmentOut,
    BudgetLineIn,
    BudgetLineOut,
    BudgetOut,
    CategorySpendOut,
    CommitmentMatchIn,
    CommitmentPaymentIn,
    LearningNeedsOut,
    LearningOut,
    UnbudgetedOut,
)
from app.services import budget as service
from app.services import commitments, rollups
from app.services.capabilities import currency_for, require_feature
from app.services.conflicts import log_conflict
from app.services.dashboard import month_bounds, parse_month
from app.services.identity import ResolvedIdentity
from app.services.learning import LearningState, learning_state
from app.services.ledger import ExactDuplicate, save_manual

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

    def maybe(minor: int | None) -> str | None:
        return None if minor is None else money(minor)

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
            ai_suggested=maybe(item.ai_suggested),
            ai_reason=item.ai_reason,
            one_off=money(item.one_off) if item.one_off else None,
            this_month_only=item.this_month_only,
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
        unbudgeted=[
            UnbudgetedOut(
                category_id=u.category_id,
                slug=u.slug,
                name=u.name,
                spent=money(u.spent),
                ai_suggested=maybe(u.ai_suggested),
                ai_reason=u.ai_reason,
                one_off=money(u.one_off) if u.one_off else None,
            )
            for u in view.unbudgeted
        ],
        unbudgeted_spent=money(view.unbudgeted_spent),
        spent_overall=money(view.spent_overall),
        month_income=money(view.month_income),
        month_spending=money(view.month_spending),
        month_net=money(view.month_income - view.month_spending),
        commitments=[
            BudgetCommitmentOut(
                name=c.name,
                expected=money(c.expected_minor_units),
                due_day=c.due_day,
                category=c.category_slug,
                paid_on=c.match.occurred_on if c.match else None,
                paid_amount=money(c.match.amount_minor_units) if c.match else None,
                id=c.obligation_id,
                transaction_id=c.match.transaction_id if c.match else None,
                paid_by_hand=c.by_hand,
            )
            for c in view.commitments
        ],
        month_ended=view.month_ended,
        spending_by_category=[
            CategorySpendOut(
                category_id=c.category_id,
                slug=c.slug,
                name=c.name,
                spent=money(c.spent_minor_units),
            )
            for c in view.spending_by_category
        ],
        income_expected=maybe(view.income_expected),
        commitments_due=maybe(view.commitments_due),
        budget_rest=maybe(view.budget_rest),
        savings_due=maybe(view.savings_due),
        projected_left=maybe(view.projected_left),
        month_saved=money(view.month_saved),
        month_left=money(view.month_income - view.month_spending - view.month_saved),
        savings_room=money(view.savings_room),
        savings_category_id=view.savings_category_id,
        ai_kind=view.ai_kind,
        ai_extra=[
            AiLineOut(
                category_id=a.category_id,
                slug=a.slug,
                name=a.name,
                amount=money(a.amount),
                reason=a.reason,
            )
            for a in view.ai_extra
        ],
        ai_savings=maybe(view.ai_savings),
        ai_advice=view.ai_advice,
        ai_review_due=view.ai_review_due,
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
    """Set one line by hand. Regeneration will not move it again; it carries
    into the months after unless `this_month_only`."""
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
        this_month_only=body.this_month_only,
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


# --- Ticking a commitment off by hand ---------------------------------------

ALREADY_PAYS_ANOTHER = "already_pays_another_commitment"
# The same code `POST /transactions` answers an exact copy with.
DUPLICATE_TRANSACTION = "duplicate_transaction"


async def _obligation(
    session: AsyncSession, household_id: uuid.UUID, obligation_id: uuid.UUID
) -> Obligation:
    row = await session.scalar(
        select(Obligation).where(
            Obligation.id == obligation_id, Obligation.household_id == household_id
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"}
        )
    return row


def _outside(month: date, occurred_on: date) -> HTTPException | None:
    first, last = month_bounds(month)
    if first <= occurred_on <= last:
        return None
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "code": "not_in_month",
            "field": "occurred_on",
            "message": "A payment for this month has to fall in it.",
        },
    )


async def _link(
    session: AsyncSession,
    household_id: uuid.UUID,
    month: date,
    obligation: Obligation,
    transaction: Transaction,
) -> None:
    """Name [transaction] as what paid [obligation] in [month], replacing any
    earlier name — unless it already pays another commitment (409)."""
    key = commitments.key_of(obligation.name)
    other = await session.scalar(
        select(CommitmentPayment).where(
            CommitmentPayment.transaction_id == transaction.id,
        )
    )
    if other is not None and not (other.month == month and other.commitment_key == key):
        log_conflict(
            ALREADY_PAYS_ANOTHER,
            "transaction_already_ticks_off_a_commitment",
            household_id=str(household_id),
            transaction_id=str(transaction.id),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": ALREADY_PAYS_ANOTHER,
                "message": "That payment already ticks off another commitment.",
            },
        )
    await session.execute(
        delete(CommitmentPayment).where(
            CommitmentPayment.household_id == household_id,
            CommitmentPayment.month == month,
            CommitmentPayment.commitment_key == key,
        )
    )
    session.add(
        CommitmentPayment(
            household_id=household_id,
            month=month,
            commitment_key=key,
            transaction_id=transaction.id,
        )
    )
    await session.flush()


async def _answer(
    session: AsyncSession, identity: ResolvedIdentity, month: date
) -> BudgetOut:
    today = _today()
    household = identity.household
    currency = await currency_for(session, household)
    learning = await learning_state(session, household.id, currency, today)
    view = await service.budget_for(
        session, household.id, currency, month, today, ready=learning.ready
    )
    await session.commit()
    return _out(view, learning)


@router.put("/{month}/commitments/{obligation_id}/payment", response_model=BudgetOut)
async def tick_off_with_transaction(
    month: str,
    obligation_id: uuid.UUID,
    body: CommitmentMatchIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> BudgetOut:
    """Tick a commitment off with a payment already recorded this month.

    Nothing new is counted: the transaction is already in the month's
    spending, and now it is also what settled this commitment.
    """
    when = _month(month)
    household_id = identity.household.id
    currency = await currency_for(session, identity.household)
    obligation = await _obligation(session, household_id, obligation_id)
    transaction = await session.scalar(
        select(Transaction).where(
            Transaction.id == body.transaction_id,
            Transaction.household_id == household_id,
        )
    )
    if transaction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"}
        )
    if (
        transaction.direction != TransactionDirection.debit
        or transaction.currency != currency
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "not_spending",
                "field": "transaction_id",
                "message": "Only money going out can pay a commitment.",
            },
        )
    if (refused := _outside(when, transaction.occurred_on)) is not None:
        raise refused
    await _link(session, household_id, when, obligation, transaction)
    return await _answer(session, identity, when)


@router.post("/{month}/commitments/{obligation_id}/payment", response_model=BudgetOut)
async def tick_off_with_new_payment(
    month: str,
    obligation_id: uuid.UUID,
    body: CommitmentPaymentIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> BudgetOut:
    """Tick a commitment off with a payment typed in now.

    The payment is recorded as a transaction — the commitment's name, its
    category, money going out — exactly as `POST /transactions` would, and
    named as what paid the commitment. So it is counted once: as spending in
    its line, and as this commitment paid. An exact copy of a recorded row is
    refused the same way, naming it, so the person can tick off with that
    one instead.
    """
    when = _month(month)
    household_id = identity.household.id
    obligation = await _obligation(session, household_id, obligation_id)
    if (refused := _outside(when, body.occurred_on)) is not None:
        raise refused
    account = await session.scalar(
        select(Account).where(
            Account.id == body.account_id, Account.household_id == household_id
        )
    )
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"}
        )
    shared = await commitments.shared_categories(session)
    slug = commitments.slug_for(obligation, {id_: s for s, id_ in shared.items()})
    try:
        row = await save_manual(
            session,
            household_id=household_id,
            account=account,
            occurred_on=body.occurred_on,
            amount=body.amount,
            direction=TransactionDirection.debit,
            description=obligation.name,
            category_id=shared.get(slug) if slug else None,
            allow_duplicate=body.allow_duplicate,
        )
    except MoneyError as error:
        raise _invalid_amount(str(error)) from error
    except ExactDuplicate as duplicate:
        match = duplicate.match
        log_conflict(
            DUPLICATE_TRANSACTION,
            "commitment_payment_matches_existing_transaction",
            household_id=str(household_id),
            transaction_id=str(match.id),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": DUPLICATE_TRANSACTION,
                "message": "You already have this transaction.",
                "duplicate_of": {
                    "id": str(match.id),
                    "occurred_on": match.occurred_on.isoformat(),
                    "amount": from_minor_units(
                        match.amount_minor_units, match.currency
                    ),
                    "description": match.description,
                },
            },
        ) from duplicate
    await session.flush()
    await rollups.refresh(session, household_id, [row.occurred_on])
    await _link(session, household_id, when, obligation, row)
    return await _answer(session, identity, when)


@router.delete("/{month}/commitments/{obligation_id}/payment", response_model=BudgetOut)
async def untick(
    month: str,
    obligation_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> BudgetOut:
    """Undo a tick made by hand. The transaction stays; it simply no longer
    names this commitment, which goes back to being matched automatically."""
    when = _month(month)
    household_id = identity.household.id
    obligation = await _obligation(session, household_id, obligation_id)
    await session.execute(
        delete(CommitmentPayment).where(
            CommitmentPayment.household_id == household_id,
            CommitmentPayment.month == when,
            CommitmentPayment.commitment_key == commitments.key_of(obligation.name),
        )
    )
    return await _answer(session, identity, when)
