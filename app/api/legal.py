"""The legal copy a user is shown, and the consent they give to it.

Two policies live here and are kept apart on purpose: the account terms, agreed
at signup, and consent to AI processing of financial data, asked before the
first statement import. Appendix A.5 #1 requires the second to be express and
unbundled — one screen covering both would be neither.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.auth import AuthenticatedUser, current_user
from app.db import get_session
from app.models.identity import ConsentEvent
from app.schemas.legal import ConsentAcceptedOut, PolicyConsentIn, TermsOut
from app.services.ai_consent import current_policy, has_consented
from app.services.conflicts import log_conflict
from app.services.identity import ResolvedIdentity
from app.services.onboarding import current_terms

router = APIRouter(tags=["legal"])

NO_TERMS = "no_terms"
NO_AI_POLICY = "no_ai_policy"
AI_POLICY_VERSION_MISMATCH = "ai_policy_version_mismatch"


@router.get("/legal/terms", response_model=TermsOut)
async def read_terms(
    _caller: AuthenticatedUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TermsOut:
    """The terms in force, for the consent screen.

    Signed-in callers only, but depends on `current_user` rather than
    `current_identity`: reading the terms creates nothing.
    """
    terms = await current_terms(session)
    if terms is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": NO_TERMS, "message": "No account terms are configured."},
        )
    return TermsOut(
        version=terms.version, body=terms.body, effective_from=terms.effective_from
    )


@router.get("/legal/ai-processing", response_model=TermsOut)
async def read_ai_policy(
    _caller: AuthenticatedUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TermsOut:
    """The AI-processing policy, for the screen shown before the first import."""
    policy = await current_policy(session)
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": NO_AI_POLICY,
                "message": "No AI-processing policy is configured.",
            },
        )
    return TermsOut(
        version=policy.version, body=policy.body, effective_from=policy.effective_from
    )


@router.post("/legal/ai-processing/consent", response_model=ConsentAcceptedOut)
async def accept_ai_policy(
    body: PolicyConsentIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> ConsentAcceptedOut:
    """Record consent to AI processing, against the exact version shown.

    The client names the version it displayed. If it has changed since, the user
    agreed to text that is no longer in force — refuse rather than record
    consent to something they did not read. Accepting twice is a no-op.
    """
    policy = await current_policy(session)
    if policy is None or body.version != policy.version:
        log_conflict(
            AI_POLICY_VERSION_MISMATCH,
            "no_ai_policy_in_force" if policy is None else "stale_ai_policy_version",
            user_id=str(identity.user.id),
            sent_version=body.version,
            current_version=policy.version if policy else None,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": AI_POLICY_VERSION_MISMATCH,
                "message": "That is not the AI-processing policy currently in force.",
                "current_version": policy.version if policy else None,
            },
        )

    if not await has_consented(session, identity.user, policy):
        await session.execute(
            pg_insert(ConsentEvent)
            .values(user_id=identity.user.id, disclaimer_version_id=policy.id)
            .on_conflict_do_nothing(index_elements=["user_id", "disclaimer_version_id"])
        )
        await session.commit()

    return ConsentAcceptedOut(version=policy.version)
