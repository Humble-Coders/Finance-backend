"""POST /statements/parse — redacted text in, transaction rows out.

The server half of the 2026-09-21 decision (PRD §9). The statement is read and
redacted on the device; what arrives here is text, and the document never
existed as far as this service is concerned.

Order of refusals matters and is deliberate: feature gate, then consent, then
quota, and only then the model. Each of those is free; the model is not. A
request that was never allowed must not cost money.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.config import get_settings
from app.db import get_session
from app.models.enums import StatementImportStatus
from app.models.identity import Household
from app.models.money import Account, StatementImport, StatementImportText
from app.schemas.statements import ParsedRowOut, StatementParseIn, StatementParseOut
from app.services.ai_consent import CONSENT_REQUIRED, current_policy, has_consented
from app.services.capabilities import currency_for, require_feature
from app.services.conflicts import log_conflict
from app.services.identity import ResolvedIdentity
from app.services.llm import LlmError, build_client, close_client
from app.services.statements import MAX_TEXT_CHARS, parse_statement

router = APIRouter(tags=["statements"])

log = structlog.get_logger()

# The capability key predates the rename and stays: the mobile clients already
# read `features["document_upload"]`, and renaming a key the apps depend on to
# match a table name is a breaking change bought with nothing.
IMPORT_FEATURE = "document_upload"

QUOTA_EXCEEDED = "import_quota_exceeded"
TOO_LONG = "statement_too_long"
UNKNOWN_ACCOUNT = "unknown_account"
PARSE_FAILED = "parse_failed"

# Kept for 30 days when — and only when — the user opted in after a bad import.
DIAGNOSTIC_TEXT_DAYS = 30
# Below this, an import is "mostly flagged" and the diagnostic offer is honest.
# Above it the parse worked, and keeping the text would be collecting data we
# said we would not keep.
DIAGNOSTIC_MIN_FAILURE_RATIO = 0.5


async def _purge_expired(session: AsyncSession) -> None:
    """Delete diagnostic text past its date.

    Done here, on the path that creates it, rather than by a scheduled job. A
    retention promise that depends on a cron nobody watches is how "deleted
    after 30 days" quietly becomes false — and the on-device decision removed
    the worker that would have run it anyway.
    """
    await session.execute(
        delete(StatementImportText).where(
            StatementImportText.expires_at < datetime.now(UTC)
        )
    )


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
    client = build_client(settings)
    try:
        outcome = await parse_statement(client, body.text, currency)
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
