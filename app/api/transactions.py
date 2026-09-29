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
    ConfirmIn,
    ConfirmOutcomeOut,
    DeleteOutcomeOut,
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
        row.merchant = body.merchant

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


@router.post("/transactions/confirm", response_model=ConfirmOutcomeOut)
async def confirm_transactions(
    body: ConfirmIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> ConfirmOutcomeOut:
    """Accept many rows as extracted, all or nothing.

    Every id is checked before anything is written. One that is not this
    household's — someone else's row, or one that does not exist — fails the
    whole request with nothing applied. Partly confirming a list and then
    refusing the rest would leave the user unsure which of their taps landed,
    and a list that reached another household's ids was built wrongly anyway.
    """
    household_id = identity.household.id
    wanted = set(body.ids)

    # Locked for the rest of the request: between this check and the write, a
    # row must not be deleted or moved out from under the answer being given.
    result = await session.execute(
        select(Transaction)
        .where(
            Transaction.id.in_(wanted),
            Transaction.household_id == household_id,
        )
        # In id order, so two overlapping requests take their locks in the
        # same order. Unordered, each can hold a row the other is waiting for,
        # and Postgres ends the stand-off by failing one of them with a 500.
        .order_by(Transaction.id)
        .with_for_update()
    )
    rows = list(result.scalars().all())
    if len(rows) != len(wanted):
        # Which ids failed is deliberately not said: naming them would tell a
        # caller which guessed ids exist in somebody else's household.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"}
        )

    waiting = [row for row in rows if row.needs_review]
    for row in waiting:
        _resolve(row)
    await session.flush()

    finished = []
    for import_id in {row.statement_import_id for row in waiting} - {None}:
        if await _finish_import(session, import_id):
            finished.append(import_id)
    await session.commit()
    return ConfirmOutcomeOut(confirmed=len(waiting), imports_finished=finished)


@router.post("/transactions/{transaction_id}/confirm", response_model=PatchOutcomeOut)
async def confirm_transaction(
    transaction_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> PatchOutcomeOut:
    """Accept one row as extracted. Idempotent: confirming twice is fine."""
    household_id = identity.household.id
    row = await _owned(session, household_id, transaction_id)
    import_id = row.statement_import_id
    was_waiting = row.needs_review

    _resolve(row)
    await session.flush()

    finished = was_waiting and await _finish_import(session, import_id)
    await session.commit()
    return PatchOutcomeOut(transaction=_as_out(row), import_finished=finished)


@router.delete("/transactions/{transaction_id}", response_model=DeleteOutcomeOut)
async def delete_transaction(
    transaction_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> DeleteOutcomeOut:
    """Remove a row that was never a transaction.

    A header the parser mistook for a line, or a copy the user recognises. It
    is their own data and they are saying it is wrong, so it goes — a hard
    delete, not a flag that every later query would have to remember to skip.

    Not limited to rows still in the queue: a bogus row found after confirming
    it is just as bogus.

    Nothing else is deleted with it. The two things that can point at a
    transaction both let go rather than follow it: a suspected duplicate that
    matched this row keeps its own place in the queue, and a rule learned from
    this row keeps teaching, because the rule was about the merchant, not
    about this line.
    """
    household_id = identity.household.id
    row = await _owned(session, household_id, transaction_id)
    import_id, was_waiting = row.statement_import_id, row.needs_review

    await session.delete(row)
    await session.flush()

    # Deleting a row still waiting is one way of answering it, so it can be the
    # answer that finishes an import. Deleting one already confirmed changes
    # nothing about what is outstanding.
    finished = was_waiting and await _finish_import(session, import_id)
    await session.commit()
    return DeleteOutcomeOut(import_finished=finished)


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
