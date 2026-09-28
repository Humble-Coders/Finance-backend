"""The review queue — the rows extraction could not resolve on its own.

Every row here is a question for a person: a figure the model was unsure of, a
merchant it could not place, or something that looks like a transaction already
recorded. There is no vision fallback and no second model pass; this queue is
where "we could not tell" goes (PRD F2 stage 7).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.core.money import MoneyError, from_minor_units, to_minor_units
from app.db import get_session
from app.models.categorization import Category
from app.models.money import StatementImport, Transaction
from app.schemas.transactions import (
    DuplicateOfOut,
    PatchOutcomeOut,
    ReviewPageOut,
    ReviewRowOut,
    TransactionOut,
    TransactionPatchIn,
)
from app.services.conflicts import log_conflict
from app.services.corrections import learn
from app.services.identity import ResolvedIdentity
from app.services.ledger import collides_with
from app.services.normalization import merchant as readable_merchant
from app.services.normalization import normalized
from app.services.review import PAGE_SIZE, Cursor, CursorError, stamp_if_finished

router = APIRouter(tags=["transactions"])

WOULD_DUPLICATE = "would_duplicate"
DEDUP_INDEX = "uq_transaction_dedup"


@router.get("/transactions/review", response_model=ReviewPageOut)
async def review_queue(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=PAGE_SIZE, ge=1, le=PAGE_SIZE),
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> ReviewPageOut:
    """This household's outstanding rows, newest first.

    Ordered by the date on the statement rather than when the row was written:
    somebody reviewing an import is reading a statement, and a queue ordered by
    insertion time interleaves two statements imported minutes apart into
    something that matches neither.
    """
    where = [
        Transaction.household_id == identity.household.id,
        Transaction.needs_review.is_(True),
    ]

    if cursor is not None:
        try:
            after = Cursor.decode(cursor)
        except CursorError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "invalid_cursor"},
            ) from error
        # Strictly after the last row of the previous page, in the same order
        # the rows come back in. The id half only decides ties on the date, so
        # rows sharing a date are still walked exactly once.
        where.append(
            or_(
                Transaction.occurred_on < after.occurred_on,
                (Transaction.occurred_on == after.occurred_on)
                & (Transaction.id < after.transaction_id),
            )
        )

    # One row beyond the page, to know whether another page exists without
    # counting the queue. A count would be a second query answering a question
    # nobody asked: the client needs "is there more", not "how many".
    result = await session.execute(
        select(Transaction)
        .where(*where)
        .order_by(Transaction.occurred_on.desc(), Transaction.id.desc())
        .limit(limit + 1)
    )
    found = list(result.scalars().all())
    rows, has_more = found[:limit], len(found) > limit

    duplicates = await _duplicates_for(session, identity.household.id, rows)

    return ReviewPageOut(
        rows=[_as_row(row, duplicates.get(row.duplicate_of_id)) for row in rows],
        next_cursor=(
            Cursor(
                occurred_on=rows[-1].occurred_on, transaction_id=rows[-1].id
            ).encode()
            if has_more and rows
            else None
        ),
    )


async def _duplicates_for(
    session: AsyncSession,
    household_id: uuid.UUID,
    rows: list[Transaction],
) -> dict[uuid.UUID, Transaction]:
    """The rows that suspected duplicates matched, fetched once for the page.

    Scoped to the household as well as the ids: `duplicate_of_id` is a foreign
    key to `transaction` at large, and a corrupted or stale one must not become
    a way to read somebody else's row.
    """
    wanted = {row.duplicate_of_id for row in rows if row.duplicate_of_id}
    if not wanted:
        return {}

    result = await session.execute(
        select(Transaction).where(
            Transaction.id.in_(wanted),
            Transaction.household_id == household_id,
        )
    )
    return {found.id: found for found in result.scalars().all()}


def _as_row(row: Transaction, duplicate: Transaction | None) -> ReviewRowOut:
    return ReviewRowOut(
        id=row.id,
        account_id=row.account_id,
        occurred_on=row.occurred_on,
        amount=from_minor_units(row.amount_minor_units, row.currency),
        currency=row.currency,
        direction=row.direction,
        description=row.description,
        merchant=row.merchant,
        category_id=row.category_id,
        review_reason=row.review_reason,
        extraction_confidence=row.extraction_confidence,
        duplicate_of=(
            DuplicateOfOut(
                id=duplicate.id,
                occurred_on=duplicate.occurred_on,
                amount=from_minor_units(
                    duplicate.amount_minor_units, duplicate.currency
                ),
                description=duplicate.description,
            )
            if duplicate is not None
            else None
        ),
    )


@router.patch("/transactions/{transaction_id}", response_model=PatchOutcomeOut)
async def correct_transaction(
    transaction_id: uuid.UUID,
    body: TransactionPatchIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> PatchOutcomeOut:
    """A person fixing what extraction got wrong.

    Answering is the point: whatever was changed, the row has now been looked
    at, so it leaves the queue.
    """
    household_id = identity.household.id
    row = await _owned(session, household_id, transaction_id)

    occurred_on = body.occurred_on or row.occurred_on
    description = body.description if body.description is not None else row.description
    try:
        minor = (
            to_minor_units(body.amount, row.currency)
            if body.amount is not None
            else row.amount_minor_units
        )
    except MoneyError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_amount", "field": "amount", "message": str(error)},
        ) from error
    key = (
        normalized(description)
        if body.description is not None
        else row.normalized_description
    )

    # Every part of the dedup key a person can edit. A typo fixed in the amount
    # can turn this row into a copy of one already recorded — and the database
    # would refuse the write, so the useful thing is to say so first, naming
    # the row it would duplicate so the screen can offer to delete this one.
    if (occurred_on, minor, key) != (
        row.occurred_on,
        row.amount_minor_units,
        row.normalized_description,
    ):
        clash = await collides_with(
            session,
            transaction=row,
            occurred_on=occurred_on,
            amount_minor_units=minor,
            normalized_description=key,
        )
        if clash is not None:
            _refuse_duplicate(household_id, row.id, clash.id)

    # Recorded before the row changes, because the correction is precisely the
    # difference between the two. Setting the category it already has is the
    # user agreeing with the categorizer, which teaches nothing.
    predicted_category_id = row.category_id
    recategorize = (
        body.category_id is not None and body.category_id != predicted_category_id
    )
    if body.category_id is not None:
        await _visible_category(session, household_id, body.category_id)
        row.category_id = body.category_id

    row.occurred_on = occurred_on
    row.amount_minor_units = minor
    if body.direction is not None:
        row.direction = body.direction
    if body.description is not None:
        row.description = description
        row.normalized_description = key
        # The merchant is derived from the description, so a new description
        # re-derives it — unless the user named the merchant themselves, in
        # which case theirs wins.
        if body.merchant is None:
            row.merchant = readable_merchant(description)
    if body.merchant is not None:
        row.merchant = body.merchant.strip()

    _resolve(row)

    row_id, import_id = row.id, row.statement_import_id
    try:
        await session.flush()
    except IntegrityError as exc:
        # The check above and this write race each other; the index is what
        # actually holds, so it is what the answer comes from.
        await session.rollback()
        if DEDUP_INDEX not in str(exc.orig):
            raise
        _refuse_duplicate(household_id, row_id, None)

    # After the row is written, so the rule learns the merchant the user just
    # confirmed rather than the one they were correcting.
    recategorized, rule_recorded = 0, False
    if recategorize:
        learned = await learn(
            session,
            household_id=household_id,
            row=row,
            predicted_category_id=predicted_category_id,
            corrected_category_id=body.category_id,
        )
        recategorized, rule_recorded = learned.recategorized, learned.rule_recorded

    finished = await _finish_import(session, import_id)
    await session.commit()
    return PatchOutcomeOut(
        transaction=_as_out(row),
        import_finished=finished,
        rule_recorded=rule_recorded,
        recategorized=recategorized,
    )


async def _owned(
    session: AsyncSession, household_id: uuid.UUID, transaction_id: uuid.UUID
) -> Transaction:
    """The row, if it is this household's — otherwise 404, never 403.

    A 403 would confirm that the id exists and belongs to someone else, which
    is a way to probe for other people's transactions one guess at a time.
    """
    result = await session.execute(
        select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.household_id == household_id,
        )
    )
    row = result.scalars().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"}
        )
    return row


async def _visible_category(
    session: AsyncSession, household_id: uuid.UUID, category_id: uuid.UUID
) -> Category:
    """A category this household may file into: a system one, or its own.

    Another household's category is answered exactly like one that does not
    exist, for the same reason as `_owned`.
    """
    result = await session.execute(
        select(Category).where(
            Category.id == category_id,
            or_(
                Category.household_id.is_(None),
                Category.household_id == household_id,
            ),
        )
    )
    category = result.scalars().first()
    if category is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "unknown_category", "field": "category_id"},
        )
    return category


def _resolve(row: Transaction) -> None:
    """The row has been answered; it leaves the queue.

    `duplicate_of_id` goes with it. The pointer is evidence for an open
    question — "is this the same as that?" — and a row kept after that question
    was asked has been answered "no". Leaving the pointer would keep asserting
    a suspicion the user has already rejected.
    """
    row.needs_review = False
    row.review_reason = None
    row.duplicate_of_id = None


async def _finish_import(session: AsyncSession, import_id: uuid.UUID | None) -> bool:
    if import_id is None:
        return False
    record = await session.get(StatementImport, import_id)
    already = record is not None and record.confirmed_at is not None
    remaining = await stamp_if_finished(session, record)
    return record is not None and remaining == 0 and not already


def _refuse_duplicate(
    household_id: uuid.UUID, row_id: uuid.UUID, clash_id: uuid.UUID | None
) -> None:
    log_conflict(
        WOULD_DUPLICATE,
        "edit_matches_existing_transaction",
        household_id=str(household_id),
        transaction_id=str(row_id),
    )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": WOULD_DUPLICATE,
            "message": (
                "That change makes this a copy of a transaction you already have."
            ),
            "duplicate_of": str(clash_id) if clash_id else None,
        },
    )


def _as_out(row: Transaction) -> TransactionOut:
    return TransactionOut(
        **_as_row(row, None).model_dump(),
        needs_review=row.needs_review,
    )
