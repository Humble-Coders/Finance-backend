"""The legal copy a user is shown, and the consent they give to it — or take
back (#42).

Two policies live here and are kept apart on purpose: the account terms, agreed
at signup, and consent to AI processing of financial data, asked before the
first statement import. Appendix A.5 #1 requires the second to be express and
unbundled — one screen covering both would be neither.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.auth import AuthenticatedUser, current_user
from app.db import get_session
from app.schemas.legal import (
    ConsentAcceptedOut,
    ConsentStatusOut,
    PolicyConsentIn,
    TermsOut,
)
from app.services.ai_consent import (
    current_policy,
    has_consented,
    record_consent,
    withdraw,
)
from app.services.conflicts import log_conflict
from app.services.goals import regional_disclaimer
from app.services.identity import ResolvedIdentity
from app.services.onboarding import current_terms

router = APIRouter(tags=["legal"])
log = structlog.get_logger()

NO_TERMS = "no_terms"
NO_DISCLAIMER = "no_disclaimer"
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


@router.get("/legal/disclaimer", response_model=TermsOut)
async def read_disclaimer(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> TermsOut:
    """The household's regional disclaimer, the same shape as the terms.

    From the server, not the app bundle (PRD §4.6): wording a regulator may
    need changed has to be changeable without an app release. Goals' long-term
    projections are the first screen to show it (PRD F5); the dashboard will
    too, so it is not gated by any one feature.
    """
    disclaimer = await regional_disclaimer(session, identity.household)
    if disclaimer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": NO_DISCLAIMER,
                "message": "No disclaimer is configured for this region.",
            },
        )
    return TermsOut(
        version=disclaimer.version,
        body=disclaimer.body,
        effective_from=disclaimer.effective_from,
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

    # Also how consent comes back after a withdrawal — to this same version,
    # if it is still the one in force (#42).
    if await record_consent(session, identity.user, policy):
        log.info("ai_consent_given", user_id=str(identity.user.id))
    await session.commit()

    return ConsentAcceptedOut(version=policy.version)


@router.get("/legal/ai-processing/consent", response_model=ConsentStatusOut)
async def ai_consent_status(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> ConsentStatusOut:
    """Whether this person currently consents to the policy in force.

    What the Settings row reads to offer "withdraw" or "give consent". False
    when the policy changed since they agreed: consent to old text is not
    consent to the new one, and the row should say so.
    """
    policy = await current_policy(session)
    consented = policy is not None and await has_consented(
        session, identity.user, policy
    )
    return ConsentStatusOut(
        consented=consented, version=policy.version if policy else None
    )


@router.delete("/legal/ai-processing/consent", response_model=ConsentStatusOut)
async def withdraw_ai_consent(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> ConsentStatusOut:
    """Withdraw consent to AI processing (#42) — PIPEDA's right, at any time.

    Idempotent: withdrawing twice, or without ever having consented, answers
    the same 200 and records nothing new. From here on `/statements/parse`
    answers `409 consent_required`, which the app already handles by showing
    the consent screen, and a typed-in transaction is filed by the household's
    own rules or left for the person rather than sent to a model (#38).

    What it does **not** do: delete a transaction. Those are the person's own
    financial records; removing them is account deletion (Appendix A.5 §3).
    The consent that was given is not deleted either — it is evidence that the
    processing before this moment was agreed to.
    """
    if await withdraw(session, identity.user):
        log.info("ai_consent_withdrawn", user_id=str(identity.user.id))
    await session.commit()
    policy = await current_policy(session)
    return ConsentStatusOut(consented=False, version=policy.version if policy else None)
