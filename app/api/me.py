"""/me — who the caller is, and the onboarding answers they give.

`GET /me` creates the user and household on first call. A GET with side effects
is unconventional; it is a get-or-create, it is idempotent, and it saves every
client a separate bootstrap round trip on the path users hit most.

The onboarding writes (app/services/onboarding.py) — choosing a region and
accepting the account terms — return the updated `/me`, so the client routes on
the response without another call. Profile's: `PATCH /me` changes the name, and
`DELETE /me` deletes the account and its sign-in (app/services/account_deletion.py).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.config import Settings, get_settings
from app.db import get_session
from app.models.enums import RegionSource
from app.models.identity import ConsentEvent, HouseholdRegionChange
from app.schemas.identity import (
    ConsentIn,
    HouseholdOut,
    MeOut,
    ProfileIn,
    RegionIn,
    TermsStatus,
    UserOut,
)
from app.services.account_deletion import SignInDeletionError, delete_account
from app.services.conflicts import log_conflict
from app.services.identity import ResolvedIdentity
from app.services.onboarding import onboarding_state
from app.services.region import normalize_region

router = APIRouter(tags=["identity"])

UNKNOWN_REGION = "unknown_region"
TERMS_VERSION_MISMATCH = "terms_version_mismatch"


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
        # Version strings are ours, not the user's, so they are safe to log —
        # and which one the client showed is the only way to tell a stale app
        # from terms that were never configured.
        log_conflict(
            TERMS_VERSION_MISMATCH,
            "no_terms_in_force" if terms is None else "stale_terms_version",
            user_id=str(identity.user.id),
            sent_version=body.version,
            current_version=terms.version if terms else None,
        )
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


@router.patch("/me", response_model=MeOut)
async def update_profile(
    body: ProfileIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> MeOut:
    """The person's name, as Profile shows it. Trimmed; blank is refused."""
    name = body.display_name.strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "name_required", "field": "display_name"},
        )
    identity.user.display_name = name
    await session.commit()
    return await _me(session, identity)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_me(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> None:
    """Delete the account: every row of the household (this person's own when
    someone else shares it) and the sign-in. Nothing is deleted unless the
    sign-in could be removed too, so a failure can simply be tried again."""
    try:
        await delete_account(session, settings, identity.user, identity.household)
    except SignInDeletionError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "deletion_unavailable",
                "message": (
                    "Your account couldn't be deleted just now. Nothing was "
                    "removed; try again in a moment."
                ),
            },
        ) from error
