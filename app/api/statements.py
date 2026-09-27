"""POST /statements/parse — redacted text in, transaction rows out.

The server half of the 2026-09-21 decision (PRD §9). The statement is read and
redacted on the device; what arrives here is text, and the document never
existed as far as this service is concerned.

Order of refusals matters and is deliberate: feature gate, then consent, then
quota, and only then the model. Each of those is free; the model is not. A
request that was never allowed must not cost money.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.config import get_settings
from app.core.money import from_minor_units
from app.db import get_session
from app.models.categorization import Category
from app.models.enums import ReviewReason, StatementImportStatus
from app.models.identity import Household
from app.models.money import Account, StatementImport, StatementImportText, Transaction
from app.schemas.statements import (
    ConfirmRowsIn,
    ParsedRowOut,
    SaveOutcomeOut,
    StatementImportOut,
    StatementParseIn,
    StatementParseOut,
)
from app.services.ai_consent import CONSENT_REQUIRED, current_policy, has_consented
from app.services.capabilities import currency_for, require_feature
from app.services.categorization import categorize
from app.services.conflicts import log_conflict
from app.services.identity import ResolvedIdentity
from app.services.ledger import RowToSave, RowValidationError, save_rows
from app.services.llm import LlmError, build_client, close_client
from app.services.statements import (
    MAX_ROWS,
    MAX_TEXT_CHARS,
    TooManyRowsError,
    parse_statement,
)

router = APIRouter(tags=["statements"])

log = structlog.get_logger()

# The capability key predates the rename and stays: the mobile clients already
# read `features["document_upload"]`, and renaming a key the apps depend on to
# match a table name is a breaking change bought with nothing.
IMPORT_FEATURE = "document_upload"

QUOTA_EXCEEDED = "import_quota_exceeded"
TIER_NOT_CONFIRMED = "ai_processing_unavailable"
TOO_LONG = "statement_too_long"
TOO_MANY_ROWS = "too_many_transactions"
UNKNOWN_IMPORT = "unknown_import"
INVALID_ROW = "invalid_row"
UNKNOWN_ACCOUNT = "unknown_account"
PARSE_FAILED = "parse_failed"

# Kept for 30 days when — and only when — the user opted in after a bad import.
DIAGNOSTIC_TEXT_DAYS = 30
# Below this, an import is "mostly flagged" and the diagnostic offer is honest.
# Above it the parse worked, and keeping the text would be collecting data we
# said we would not keep.
DIAGNOSTIC_MIN_FAILURE_RATIO = 0.5


async def _purge_expired(session: AsyncSession) -> None:
    """Delete diagnostic text past its date, and commit it.

    Done here, on the path that creates it, rather than by a scheduled job. A
    retention promise that depends on a cron nobody watches is how "deleted
    after 30 days" quietly becomes false — and the on-device decision removed
    the worker that would have run it anyway.

    **The commit is the point.** Without it this runs before the consent, quota
    and size gates and is then discarded by every one of them, because
    `get_session` closes without committing — a deletion that only happens on
    requests that were going to succeed anyway. It commits on its own because
    it owns nothing else: nothing is pending at this point in the request, so
    there is no other work to drag along with it.
    """
    await session.execute(
        delete(StatementImportText).where(
            StatementImportText.expires_at < datetime.now(UTC)
        )
    )
    await session.commit()


async def _imports_this_month(session: AsyncSession, household: Household) -> int:
    """How many imports this household has *had*, which is not how many it tried.

    Failed imports are excluded, and that is the whole point. The free tier
    allows one import a month; counting a failure against it would mean a
    statement we could not read costs someone their month, while the response
    tells them to try again — advice the next request refuses. It is worse in
    combination with the diagnostic-text offer, which appears only after a
    failure: the user agrees to help us fix the parser and is locked out for
    their trouble.

    The month starts at UTC midnight rather than in the household's own
    timezone, which we do not store. Somebody importing late on the 31st gets
    next month's allowance a few hours early — the error direction to prefer.
    """
    start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    result = await session.execute(
        select(func.count())
        .select_from(StatementImport)
        .where(
            StatementImport.household_id == household.id,
            StatementImport.created_at >= start,
            StatementImport.status != StatementImportStatus.failed,
        )
    )
    return int(result.scalar_one())


def _next_month(now: datetime) -> datetime:
    return (now.replace(day=28) + timedelta(days=4)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )


@router.post(
    "/statements/parse",
    response_model=StatementParseOut,
    dependencies=[Depends(require_feature(IMPORT_FEATURE))],
)
async def parse(
    body: StatementParseIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> StatementParseOut:
    """Read a statement's redacted text and return the transactions in it.

    Nothing is persisted but the import record itself — 3.3 saves the rows, once
    the user has seen them. Returning rows the user has not confirmed is the
    point: a model read them, and a model is not authority over someone's
    financial records.
    """
    settings = get_settings()
    household, user = identity.household, identity.user

    # Before the gates, deliberately. Retained text expires on a date, and a
    # household that has stopped importing must not be the reason someone
    # else's expired text survives.
    await _purge_expired(session)

    if len(body.text) > MAX_TEXT_CHARS:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": TOO_LONG,
                "message": "That statement is too long to read in one go.",
                "limit_chars": MAX_TEXT_CHARS,
            },
        )

    if settings.is_production and not settings.llm_no_training_tier:
        # The consent screen states, as fact, that the provider is contractually
        # forbidden from training on this data. Until someone sets
        # LLM_NO_TRAINING_TIER, nothing in the system makes that true — and a
        # free tier, which is what M3 develops against, permits exactly what the
        # screen says is forbidden. Refusing is the only honest answer: showing
        # a user that text and then sending their statement anyway is the
        # violation, not a step towards it.
        log.error("llm_tier_not_confirmed", provider=settings.llm_provider)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": TIER_NOT_CONFIRMED,
                "message": "Statement import is temporarily unavailable.",
            },
        )

    policy = await current_policy(session)
    if policy is None or not await has_consented(session, user, policy):
        log_conflict(
            CONSENT_REQUIRED,
            "no_ai_policy_configured" if policy is None else "consent_not_given",
            user_id=str(user.id),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": CONSENT_REQUIRED,
                "message": "Consent to AI processing is needed before importing.",
                "policy_version": policy.version if policy else None,
            },
        )

    # Check-then-act, knowingly. Two simultaneous requests from one household
    # both read zero and both proceed, costing one extra parse.
    #
    # Every tighter version is worse here. A row or advisory lock taken now is
    # held until commit — across a model call with a 180-second budget — so one
    # import would block the household's next request for minutes. Committing
    # the record before parsing releases the lock but leaves a crashed request
    # holding the month forever. A unique index expresses a limit of exactly
    # one, and the limit is configurable.
    #
    # It needs simultaneous requests from one account to trigger and costs a
    # single parse when it does. 7.1 moves quotas into entitlements, where the
    # accounting belongs and can be done in one statement.
    used = await _imports_this_month(session, household)
    if used >= settings.free_imports_per_month:
        resets_at = _next_month(datetime.now(UTC))
        log.info(
            "import_quota_exceeded",
            household_id=str(household.id),
            used=used,
            limit=settings.free_imports_per_month,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": QUOTA_EXCEEDED,
                "message": "You have used this month's imports.",
                "limit": settings.free_imports_per_month,
                "resets_at": resets_at.isoformat(),
            },
        )

    if body.account_id is not None:
        owned = await session.execute(
            select(Account.id).where(
                Account.id == body.account_id, Account.household_id == household.id
            )
        )
        if owned.scalar_one_or_none() is None:
            # 404 rather than 403: whether an account id exists is not something
            # a caller outside the household gets to learn.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": UNKNOWN_ACCOUNT, "message": "No such account."},
            )

    record = StatementImport(
        household_id=household.id,
        source_kind=body.source_kind,
        page_count=body.page_count,
        status=StatementImportStatus.processing,
    )
    session.add(record)
    await session.flush()

    currency = await currency_for(session, household)
    client = None
    try:
        # Inside the try: `build_client` raises `LlmError` for a missing key,
        # missing model or unknown provider — which is precisely the failure
        # mode of the free-tier-to-paid swap this module exists to make safe.
        # Constructed outside, that misconfiguration escapes as an unhandled
        # 500 with no import record and no reason recorded.
        client = build_client(settings)
        period = (
            (body.statement_period_start, body.statement_period_end)
            if body.statement_period_start and body.statement_period_end
            else None
        )
        outcome = await parse_statement(client, body.text, currency, period)
    except TooManyRowsError as exc:
        # Read fine, simply bigger than this endpoint handles. Saying "we could
        # not read that statement" would be both wrong and unactionable.
        record.status = StatementImportStatus.failed
        record.failure_reason = str(exc)
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": TOO_MANY_ROWS,
                "message": "That statement has more transactions than we can "
                "import in one go.",
                "limit": MAX_ROWS,
                "import_id": str(record.id),
            },
        ) from exc
    except LlmError as exc:
        record.status = StatementImportStatus.failed
        record.failure_reason = str(exc)
        await session.commit()
        log.warning(
            "statement_parse_failed",
            import_id=str(record.id),
            reason=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": PARSE_FAILED,
                "message": "We could not read that statement. Please try again.",
                "import_id": str(record.id),
            },
        ) from exc
    finally:
        if client is not None:
            await close_client(client)

    # A parse that found nothing is a failure from the only perspective that
    # matters — the person holding a statement full of transactions. Recording
    # it as `awaiting_review` would also charge them for it.
    record.status = (
        StatementImportStatus.awaiting_review
        if outcome.rows
        else StatementImportStatus.failed
    )
    if not outcome.rows:
        record.failure_reason = "no transactions found in the submitted text"
    record.extracted_count = len(outcome.rows)

    retained_until = _retain_text_if_asked(session, record, body, outcome)
    await session.commit()

    return StatementParseOut(
        import_id=record.id,
        currency=currency,
        rows=[
            ParsedRowOut(
                occurred_on=row.occurred_on,
                description=row.description,
                amount=row.amount,
                direction=row.direction,
                confidence=row.confidence,
            )
            for row in outcome.rows
        ],
        unparsed_line_count=outcome.unparsed_line_count,
        model=outcome.model,
        prompt_version=outcome.prompt_version,
        text_retained_until=retained_until.date() if retained_until else None,
    )


def _retain_text_if_asked(
    session: AsyncSession,
    record: StatementImport,
    body: StatementParseIn,
    outcome,
) -> datetime | None:
    """Keep the redacted text only when the user asked AND the import went badly.

    Both conditions, not either. The consent is for "help us fix what went
    wrong"; honouring it after a clean parse would turn a diagnostic into
    routine collection, which is exactly the thing the on-device decision was
    made to stop. A client that sets the flag on every import — by bug or by
    design — still gets nothing here.
    """
    if not body.keep_text_for_diagnostics:
        return None

    total = len(outcome.rows) + outcome.unparsed_line_count
    failed = outcome.unparsed_line_count
    went_badly = not outcome.rows or (
        total > 0 and failed / total >= DIAGNOSTIC_MIN_FAILURE_RATIO
    )
    if not went_badly:
        return None

    expires_at = datetime.now(UTC) + timedelta(days=DIAGNOSTIC_TEXT_DAYS)
    session.add(
        StatementImportText(
            household_id=record.household_id,
            statement_import_id=record.id,
            text=body.text,
            expires_at=expires_at,
        )
    )
    log.info(
        "diagnostic_text_retained", import_id=str(record.id), days=DIAGNOSTIC_TEXT_DAYS
    )
    return expires_at


__all__ = ["router", "IMPORT_FEATURE", "QUOTA_EXCEEDED", "PARSE_FAILED"]


async def _owned_import(
    session: AsyncSession, household: Household, import_id: uuid.UUID
) -> StatementImport:
    """This household's import, or 404. Never another household's, and never
    "403" — whether an id exists is not something an outsider gets to learn."""
    result = await session.execute(
        select(StatementImport).where(
            StatementImport.id == import_id,
            StatementImport.household_id == household.id,
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": UNKNOWN_IMPORT, "message": "No such import."},
        )
    return record


@router.post(
    "/statements/{import_id}/transactions",
    response_model=SaveOutcomeOut,
    dependencies=[Depends(require_feature(IMPORT_FEATURE))],
)
async def confirm_rows(
    import_id: uuid.UUID,
    body: ConfirmRowsIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> SaveOutcomeOut:
    """Save the rows the user confirmed, and say what was already there.

    One database transaction: an import lands whole or not at all. Half a
    statement is worse than none, because nothing on screen would say which
    half.

    The rows come from the client rather than from what we parsed, because the
    user may have corrected them on the review screen first. What they said
    outranks what the model read.
    """
    settings = get_settings()
    household = identity.household
    record = await _owned_import(session, household, import_id)

    owned = await session.execute(
        select(Account.id).where(
            Account.id == body.account_id, Account.household_id == household.id
        )
    )
    if owned.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": UNKNOWN_ACCOUNT, "message": "No such account."},
        )

    currency = await currency_for(session, household)
    try:
        outcome = await save_rows(
            session,
            household_id=household.id,
            account_id=body.account_id,
            statement_import_id=record.id,
            currency=currency,
            rows=[
                RowToSave(
                    occurred_on=row.occurred_on,
                    description=row.description,
                    amount=row.amount,
                    direction=row.direction,
                    confidence=row.confidence,
                )
                for row in body.rows
            ],
        )
    except RowValidationError as exc:
        # 422 naming the row, not a 500 taking the import with it. The amounts
        # here are what a person typed on the review screen, so a bad one is an
        # ordinary event — and every other row in the statement was fine.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": INVALID_ROW,
                "message": "That amount cannot be saved.",
                "field": f"rows.{exc.index}.{exc.field}",
                "reason": exc.message,
            },
        ) from exc

    if outcome.saved_ids:
        await _apply_categories(session, settings, household.id, outcome.saved_ids)

    await session.flush()
    outstanding = await session.execute(
        select(func.count())
        .select_from(Transaction)
        .where(
            Transaction.statement_import_id == record.id,
            Transaction.needs_review.is_(True),
        )
    )
    still_to_review = int(outstanding.scalar_one())

    # `confirmed_at` means "the user has finished with this import", which 3.4
    # reads to decide when a statement is done. Stamping it while rows are
    # still flagged would mark an import complete that nobody has looked at —
    # so it is set only when nothing is outstanding. 3.4 sets it as the last
    # review is resolved.
    if still_to_review == 0:
        record.confirmed_at = datetime.now(UTC)
    await session.commit()

    return SaveOutcomeOut(
        import_id=record.id,
        saved=outcome.saved,
        duplicates=outcome.duplicates,
        flagged=outcome.flagged,
        needs_review=still_to_review,
    )


async def _apply_categories(session, settings, household_id, saved_ids) -> None:
    """Categorize what was just saved, on merchant and amount alone.

    Runs after the rows exist so a model outage cannot cost the import: the
    transactions are already written, and an uncategorized row simply goes to
    review, which is where it belongs anyway.
    """
    result = await session.execute(
        select(Transaction).where(Transaction.id.in_(saved_ids))
    )
    all_rows = list(result.scalars().all())

    # A row whose description held no name — all reference numbers, say — has
    # nothing to categorize *with*. Asking the model to file `["", "5.25"]`
    # buys an answer that looks confident and cannot be better than a guess.
    # It goes straight to a person instead, which is cheaper and honest.
    def send_to_review(rows) -> None:
        """A transaction with no category belongs in front of a person.

        Every path out of this function that leaves a row uncategorized has to
        call this. Rows are written before categorization runs precisely so a
        model problem costs nothing — but "costs nothing" means the row still
        reaches somebody, not that it lands silently with an empty category
        while the review queue says all is well. M4's budgets read categories.
        """
        for row in rows:
            row.needs_review = True
            row.review_reason = row.review_reason or ReviewReason.unknown_category

    # A row whose description held no name — all reference numbers, say — has
    # nothing to categorize *with*. Asking the model to file `["", "5.25"]`
    # buys an answer that looks confident and cannot be better than a guess.
    send_to_review([row for row in all_rows if not row.merchant])

    rows = [row for row in all_rows if row.merchant]
    if not rows:
        return

    try:
        # Inside the try: `build_client` raises LlmError for a missing key,
        # model or provider. Outside it, that misconfiguration propagates, the
        # transaction rolls back, and the import the user just confirmed is
        # lost to a 500 — which is the opposite of why categorization runs
        # after the rows are written. `categorize` already degrades on its own
        # once it has a client; this is the same promise, one step earlier.
        client = build_client(settings)
    except LlmError:
        # The likely failure, not the exotic one: a misspelled LLM_PROVIDER is
        # a deployment mistake somebody makes once. Without this, a month of
        # imports would land with no categories and nothing in the review queue
        # saying so — and `categorize` flags this same condition when it fails
        # further in, so the two paths disagreed about the same event.
        log.warning("categorization_skipped", reason="client_unavailable")
        send_to_review(rows)
        return

    try:
        # The entire payload: a shop name and a price. Nothing else may be
        # added here (PRD Appendix A.3) — there is a test that asserts it.
        suggestions = await categorize(
            session,
            client,
            household_id=household_id,
            pairs=[
                (
                    row.merchant or "",
                    from_minor_units(row.amount_minor_units, row.currency),
                )
                for row in rows
            ],
        )
    finally:
        await close_client(client)

    slugs = await session.execute(
        select(Category.slug, Category.id).where(Category.household_id.is_(None))
    )
    by_slug = {slug: ident for slug, ident in slugs.all()}

    for row, suggestion in zip(rows, suggestions, strict=False):
        row.category_id = by_slug.get(suggestion.slug)
        if not suggestion.recognised:
            row.needs_review = True
            row.review_reason = row.review_reason or ReviewReason.unknown_category


@router.get("/statements/{import_id}", response_model=StatementImportOut)
async def read_import(
    import_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> StatementImportOut:
    """How an import turned out: what was saved, what still needs a person."""
    record = await _owned_import(session, identity.household, import_id)
    counts = await session.execute(
        select(
            func.count(Transaction.id),
            func.count(Transaction.id).filter(Transaction.needs_review.is_(True)),
        ).where(Transaction.statement_import_id == record.id)
    )
    saved, needs_review = counts.one()
    return StatementImportOut(
        id=record.id,
        status=record.status,
        source_kind=record.source_kind,
        page_count=record.page_count,
        extracted_count=record.extracted_count,
        saved=int(saved),
        needs_review=int(needs_review),
        confirmed_at=record.confirmed_at,
    )
