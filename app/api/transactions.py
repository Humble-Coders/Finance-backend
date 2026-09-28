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
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.core.money import from_minor_units
from app.db import get_session
from app.models.money import Transaction
from app.schemas.transactions import DuplicateOfOut, ReviewPageOut, ReviewRowOut
from app.services.identity import ResolvedIdentity
from app.services.review import PAGE_SIZE, Cursor, CursorError

router = APIRouter(tags=["transactions"])


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
