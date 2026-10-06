"""The review queue — the rows extraction could not resolve on its own.

Every row here is a question for a person: a figure the model was unsure of, a
merchant it could not place, or something that looks like a transaction already
recorded. There is no vision fallback and no second model pass; this queue is
where "we could not tell" goes (PRD F2 stage 7).
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.config import Settings, get_settings
from app.core.money import MoneyError, from_minor_units, to_minor_units
from app.db import get_session
from app.models.categorization import Category
from app.models.enums import ReviewReason, TransactionDirection
from app.models.money import Account, StatementImport, Transaction
from app.schemas.transactions import (
    ConfirmIn,
    ConfirmOutcomeOut,
    DeleteOutcomeOut,
    DuplicateOfOut,
    ManualTransactionIn,
    PatchOutcomeOut,
    ReviewPageOut,
    ReviewRowOut,
    TransactionOut,
    TransactionPatchIn,
)
from app.services.ai_consent import current_policy, has_consented
from app.services.conflicts import log_conflict
from app.services.corrections import learn
from app.services.dashboard import month_bounds, parse_month
from app.services.filing import file_rows
from app.services.identity import ResolvedIdentity
from app.services.ledger import ExactDuplicate, collides_with, save_manual
from app.services.normalization import merchant as readable_merchant
from app.services.normalization import normalized
from app.services.review import PAGE_SIZE, Cursor, CursorError, stamp_if_finished

router = APIRouter(tags=["transactions"])

WOULD_DUPLICATE = "would_duplicate"
DUPLICATE_TRANSACTION = "duplicate_transaction"
UNKNOWN_ACCOUNT = "unknown_account"
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
    return await _page(
        session=session,
        household_id=identity.household.id,
        needs_review=True,
        statement_import_id=None,
        month=None,
        cursor=cursor,
        limit=limit,
    )


@router.get("/transactions", response_model=ReviewPageOut)
async def list_transactions(
    statement_import_id: uuid.UUID | None = Query(default=None),
    month: str | None = Query(default=None, description="YYYY-MM"),
    needs_review: bool | None = Query(default=None),
    direction: TransactionDirection | None = Query(
        default=None, description="credit (money in) or debit (money out)"
    ),
    category: str | None = Query(
        default=None, max_length=64, description="A category slug, e.g. debt_payment"
    ),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=PAGE_SIZE, ge=1, le=PAGE_SIZE),
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> ReviewPageOut:
    """This household's rows, newest first — filed ones included.

    `/transactions/review` answers "what still needs me". This answers "what
    did you do", which until now nothing did: a row the model filed with
    confidence was saved and shown to nobody, so the one question a person
    actually has after an import — *did it get my categories right* — had no
    screen that could answer it. Confidently wrong is the failure mode that
    matters, and it was the invisible one.

    `statement_import_id` scopes to a single import, which is the form the app
    uses straight after one. `needs_review` filters within that; omitted, the
    page holds both kinds, and each row says which it is.

    The same shape as the review queue, deliberately: the client already
    renders these rows and can already correct them, so the screen that shows
    the flagged ones can show all of them by changing a query parameter rather
    than growing a second list.
    """
    return await _page(
        session=session,
        household_id=identity.household.id,
        needs_review=needs_review,
        statement_import_id=statement_import_id,
        month=_month_or_400(month),
        direction=direction,
        category_slug=category,
        cursor=cursor,
        limit=limit,
    )


def _month_or_400(raw: str | None) -> date | None:
    """The month asked for, or None. Malformed is a 422, never silently all."""
    if raw is None:
        return None
    try:
        return parse_month(raw)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_month", "message": "Expected YYYY-MM."},
        ) from error


async def _page(
    *,
    session: AsyncSession,
    household_id: uuid.UUID,
    needs_review: bool | None,
    statement_import_id: uuid.UUID | None,
    month: date | None,
    cursor: str | None,
    limit: int,
    direction: TransactionDirection | None = None,
    category_slug: str | None = None,
) -> ReviewPageOut:
    """One page of this household's rows, newest first.

    Shared by both routes above so there is exactly one implementation of the
    ordering and the cursor. Two would be two chances to walk the keyset
    differently, and a paging bug does not announce itself — it silently skips
    a row.
    """
    where = [Transaction.household_id == household_id]

    if needs_review is not None:
        where.append(Transaction.needs_review.is_(needs_review))

    # No household check on the import id itself: the household filter above
    # already bounds the rows, so another household's import matches nothing
    # and returns an empty page rather than their data. A 404 here would be a
    # membership oracle for import ids.
    if statement_import_id is not None:
        where.append(Transaction.statement_import_id == statement_import_id)

    # By the date on the statement, not when the row was written — browsing by
    # month means the months a person recognises from their own statements.
    # Inclusive both ends, because `occurred_on` is a date: a half-open range
    # silently drops everything that happened on the 31st.
    if month is not None:
        first, last = month_bounds(month)
        where.append(Transaction.occurred_on >= first)
        where.append(Transaction.occurred_on <= last)

    # Money in or money out, for a screen about one of them: "Income" lists
    # credits, "Expenses" debits.
    if direction is not None:
        where.append(Transaction.direction == direction)

    # By slug rather than id, so "debt_payment" means the same thing for every
    # household: the seeded category, or this household's own of that slug.
    # An unknown slug matches nothing — an empty page, never every row.
    if category_slug is not None:
        where.append(
            Transaction.category_id.in_(
                select(Category.id).where(
                    Category.slug == category_slug,
                    or_(
                        Category.household_id.is_(None),
                        Category.household_id == household_id,
                    ),
                )
            )
        )

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

    duplicates = await _duplicates_for(session, household_id, rows)

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
        needs_review=row.needs_review,
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


@router.post(
    "/transactions",
    response_model=TransactionOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_transaction(
    body: ManualTransactionIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> TransactionOut:
    """A transaction the person typed in (#38).

    Since the vision fallback was removed (PRD §9, 2026-09-21) this is the only
    way in when a document cannot be read, so it writes the same row an import
    does: same normalization, same dedup key, same categorizing — differing
    only in `source = manual` and having no import behind it.

    Editing one later is `PATCH /transactions/{id}` (3.4), which already
    corrects any row the household owns; there is deliberately no second
    editing endpoint.
    """
    household_id = identity.household.id
    account = await _owned_account(session, household_id, body.account_id)
    if body.category_id is not None:
        await _visible_category(session, household_id, body.category_id)

    try:
        row = await save_manual(
            session,
            household_id=household_id,
            account=account,
            occurred_on=body.occurred_on,
            amount=body.amount,
            direction=body.direction,
            description=body.description,
            category_id=body.category_id,
            allow_duplicate=body.allow_duplicate,
        )
    except MoneyError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_amount", "field": "amount", "message": str(error)},
        ) from error
    except ExactDuplicate as duplicate:
        match = duplicate.match
        log_conflict(
            DUPLICATE_TRANSACTION,
            "manual_entry_matches_existing_transaction",
            household_id=str(household_id),
            transaction_id=str(match.id),
        )
        # Under `detail`, like every other coded error here: the phone reads
        # `detail.code` and `detail.duplicate_of` (mobile #30), and names the
        # match in full so it can ask "is this a second one?" without a
        # second request.
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

    # A category the person chose is kept exactly: only an entry without one
    # is filed, by the same rules an import is.
    if body.category_id is None:
        await file_rows(
            session,
            get_settings(),
            household_id,
            [row.id],
            may_ask_model=await _may_ask_model(session, identity, get_settings()),
        )

    await session.flush()
    await session.commit()
    await session.refresh(row)
    return _as_out(row)


async def _owned_account(
    session: AsyncSession, household_id: uuid.UUID, account_id: uuid.UUID
) -> Account:
    """The household's account, or the same 404 whether it is missing or not
    theirs — answering differently would tell a caller which ids exist."""
    result = await session.execute(
        select(Account).where(
            Account.id == account_id, Account.household_id == household_id
        )
    )
    account = result.scalars().first()
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": UNKNOWN_ACCOUNT,
                "field": "account_id",
                "message": "No such account.",
            },
        )
    return account


async def _may_ask_model(
    session: AsyncSession, identity: ResolvedIdentity, settings: Settings
) -> bool:
    """Whether a typed-in entry may be sent to the categorizing model.

    The two conditions the import path enforces at `/statements/parse`, asked
    here because nothing before this endpoint asked them: the person agreed to
    AI processing (express and unbundled, PRD Appendix A.5), and in production
    the provider is on a tier that does not train on inputs — the consent text
    says so as fact. Someone trying FinAI with three typed entries may have
    done neither, and their rows still get the household's own rules; only the
    model is skipped, and what it would have filed goes to review instead.
    """
    if settings.is_production and not settings.llm_no_training_tier:
        return False
    policy = await current_policy(session)
    return policy is not None and await has_consented(session, identity.user, policy)


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
    # Counted by what actually left: a row with no category stays, asking for
    # one (see `_resolve`).
    left = [row for row in waiting if _resolve(row)]
    await session.flush()

    finished = []
    for import_id in {row.statement_import_id for row in waiting} - {None}:
        if await _finish_import(session, import_id):
            finished.append(import_id)
    await session.commit()
    return ConfirmOutcomeOut(confirmed=len(left), imports_finished=finished)


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


def _resolve(row: Transaction) -> bool:
    """The row has been answered; it leaves the queue — if it has a category.

    `duplicate_of_id` goes either way. The pointer is evidence for an open
    question — "is this the same as that?" — and a row kept after that question
    was asked has been answered "no". Leaving the pointer would keep asserting
    a suspicion the user has already rejected.

    **A row with no category stays**, now asking for one. A row carries a
    single reason, so one that was both a suspected duplicate and
    uncategorized — an entry typed in without AI consent (#38), or an import
    row the model could not reach — showed only the duplicate question.
    Answering it used to release the row with no category, and M4's budgets
    read categories: the silent gap `file_rows` exists to prevent, reached
    through the back door. Confirming cannot file it either, retries included;
    a category does (`PATCH`, where "other" exists for what fits nothing), or
    deleting it does.

    Returns whether the row left the queue.
    """
    row.duplicate_of_id = None
    if row.category_id is None:
        row.needs_review = True
        row.review_reason = ReviewReason.unknown_category
        return False
    row.needs_review = False
    row.review_reason = None
    return True


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
    # `needs_review` is not passed here: it is part of the row now, so spreading
    # `_as_row` already carries it and naming it again is a duplicate keyword.
    return TransactionOut(
        **_as_row(row, None).model_dump(),
        source=row.source,
    )
