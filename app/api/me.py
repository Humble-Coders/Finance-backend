"""/me — who the caller is, and the onboarding answers they give.

`GET /me` creates the user and household on first call. A GET with side effects
is unconventional; it is a get-or-create, it is idempotent, and it saves every
client a separate bootstrap round trip on the path users hit most.

Two writes here are onboarding steps (app/services/onboarding.py): choosing a
region, and accepting the account terms. The third, `/me/link`, removes the
empty account a new sign-in method created so that method can be linked here
(app/services/account_link.py). All three return the updated `/me`, so the
client routes on the response without another call.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.auth import TokenVerifier, get_token_verifier
from app.db import get_session
from app.models.enums import RegionSource
from app.models.identity import ConsentEvent, HouseholdRegionChange
from app.schemas.identity import (
    ConsentIn,
    HouseholdOut,
    LinkIn,
    MeOut,
    RegionIn,
    TermsStatus,
    UserOut,
)
from app.services.account_link import (
    LinkTargetIncomplete,
    NothingToLink,
    OrphanCleanupFailed,
    OrphanNotEmpty,
    absorb_orphan,
)
from app.services.identity import ResolvedIdentity
from app.services.onboarding import onboarding_state
from app.services.region import normalize_region
from app.services.supabase_admin import SupabaseAdmin, get_supabase_admin

router = APIRouter(tags=["identity"])

UNKNOWN_REGION = "unknown_region"
TERMS_VERSION_MISMATCH = "terms_version_mismatch"
INVALID_ORPHAN_TOKEN = "invalid_orphan_token"
LINK_TARGET_INCOMPLETE = "link_target_incomplete"
NOTHING_TO_LINK = "nothing_to_link"
ORPHAN_NOT_EMPTY = "orphan_not_empty"
ORPHAN_CLEANUP_FAILED = "orphan_auth_cleanup_failed"


async def _me(session: AsyncSession, identity: ResolvedIdentity) -> MeOut:
    state = await onboarding_state(session, identity.user, identity.household)
    return MeOut(
        user=UserOut.model_validate(identity.user),
        household=HouseholdOut.model_validate(identity.household),
        onboarding_required=state.steps,
        terms=TermsStatus(
            version=state.terms.version if state.terms else None,
            accepted=state.terms_accepted,
        ),
    )


@router.get("/me", response_model=MeOut)
async def read_me(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> MeOut:
    return await _me(session, identity)


@router.put("/me/region", response_model=MeOut)
async def set_region(
    body: RegionIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> MeOut:
    """The region override (PRD §4.6), reachable during onboarding.

    Any known country, launched or not: signup is never blocked on region —
    features are gated instead. Every change is audited.
    """
    region = normalize_region(body.country_code)
    if region is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": UNKNOWN_REGION,
                "message": "That is not a recognised country code.",
            },
        )

    household = identity.household
    # Lock the row first, so two simultaneous changes queue up: each audit row
    # then names the region it actually replaced.
    await session.refresh(household, ["country_code"], with_for_update=True)
    if household.country_code != region:
        session.add(
            HouseholdRegionChange(
                household_id=household.id,
                previous_country_code=household.country_code,
                new_country_code=region,
                source=RegionSource.user,
                changed_by_user_id=identity.user.id,
            )
        )
        household.country_code = region
        await session.commit()

    return await _me(session, identity)


@router.post("/me/consent", response_model=MeOut)
async def accept_terms(
    body: ConsentIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> MeOut:
    """Record consent to the account terms in force (PRD Appendix A.5, item 1).

    The client names the version it showed. If the terms changed in between,
    the user agreed to text that is no longer current — refuse, and say which
    version is, rather than record consent to something they did not read.
    Accepting again is a no-op: the log records that it happened, once.
    """
    state = await onboarding_state(session, identity.user, identity.household)
    terms = state.terms
    if terms is None or body.version != terms.version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": TERMS_VERSION_MISMATCH,
                "message": "Those are not the terms currently in force.",
                "current_version": terms.version if terms else None,
            },
        )

    if not state.terms_accepted:
        # Two simultaneous accepts both get here; the unique constraint keeps the
        # log to one row, and the loser's insert quietly does nothing.
        await session.execute(
            pg_insert(ConsentEvent)
            .values(user_id=identity.user.id, disclaimer_version_id=terms.id)
            .on_conflict_do_nothing(index_elements=["user_id", "disclaimer_version_id"])
        )
        await session.commit()

    return await _me(session, identity)


@router.post("/me/link", response_model=MeOut)
async def link_account(
    body: LinkIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
    verify_token: TokenVerifier = Depends(get_token_verifier),
    admin: SupabaseAdmin = Depends(get_supabase_admin),
) -> MeOut:
    """Remove the empty account a new sign-in method created, so it can link here.

    Called signed in to the real account, with the orphan's token as proof the
    caller holds that session too. The client then links the method to this
    account in Supabase, which only succeeds once the orphan is gone.
    """
    try:
        orphan = verify_token(body.orphan_token)
    except HTTPException as exc:
        # 422, not 401: the caller's OWN session is fine. A 401 here would make
        # clients refresh their token and retry, or sign the user out.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": INVALID_ORPHAN_TOKEN,
                "message": "That sign-in session could not be verified.",
            },
        ) from exc

    try:
        await absorb_orphan(session, identity, orphan, admin)
    except LinkTargetIncomplete as exc:
        raise _conflict(
            LINK_TARGET_INCOMPLETE,
            "Verify this account's phone number before linking another sign-in.",
        ) from exc
    except NothingToLink as exc:
        raise _conflict(NOTHING_TO_LINK, "That is already this account.") from exc
    except OrphanNotEmpty as exc:
        raise _conflict(
            ORPHAN_NOT_EMPTY,
            "That account has a verified phone or data, so linking cannot remove it.",
        ) from exc
    except OrphanCleanupFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": ORPHAN_CLEANUP_FAILED,
                "message": "Linking did not finish. Try again.",
            },
        ) from exc

    return await _me(session, identity)


def _conflict(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": code, "message": message},
    )
