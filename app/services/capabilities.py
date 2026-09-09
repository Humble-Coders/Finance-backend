"""Resolving what a household may see and do.

Both mobile clients render their whole UI from the payload this produces, and
`CLAUDE.md` forbids any per-country branch in client code. That makes this the
single place market differences live — and the reason adding a country is an
INSERT rather than a release.

**One resolver, not three.** Plan entitlements, region gating and rollout flags
compose here. The PRD is explicit that they must not become separate systems,
because that is how a feature ends up enabled by one and disabled by another.

**The payload controls what is SHOWN; the API enforces what is ALLOWED.** A
hidden button is not a secured endpoint — anyone can call the API directly with
a valid token. `require_feature` is the other half, and exists now, before there
are features to gate, so the pattern is established rather than retrofitted.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_household
from app.db import get_session
from app.models.enums import PlanTier
from app.models.identity import Household
from app.models.platform import (
    CountryPack,
    FeatureAvailability,
    SubscriptionEntitlement,
)
from app.schemas.capabilities import Capabilities, Feature

__all__ = [
    "UNKNOWN_REGION_CURRENCY",
    "UNKNOWN_REGION_LOCALE",
    "resolve",
    "require_feature",
]

# Used only while the region is genuinely unknown — the window between a
# Google/Apple sign-in and the phone step. Deliberately a documented default
# rather than a guess: guessing a country shows someone the wrong tax accounts
# and the wrong disclaimer (PRD §4.6).
UNKNOWN_REGION_CURRENCY = "CAD"
UNKNOWN_REGION_LOCALE = "en-CA"

REASON_REGION_UNKNOWN = "region_unknown"
REASON_REGION_UNSUPPORTED = "region_unsupported"
REASON_NOT_IN_PLAN = "not_in_plan"
# A feature_key with no row at all. Distinct from the reasons above because it
# is a bug in our code, not a fact about the caller — reporting it as
# "region_unsupported" sends whoever debugs it looking at country packs.
REASON_UNKNOWN_FEATURE = "unknown_feature"

ONBOARDING_PHONE = "phone"


def _specificity(row: FeatureAvailability) -> int:
    """How closely a row matches, for "most specific wins".

    Country outranks plan when only one is set: region availability is usually a
    legal or operational constraint, whereas plan is commercial — so a paid plan
    must not unlock something the country does not offer.

        country + plan  3   exact
        country only    2   this market, any plan
        plan only       1   any market, this plan
        neither         0   global default
    """
    return (2 if row.country_code is not None else 0) + (
        1 if row.plan is not None else 0
    )


async def _feature_rows(
    session: AsyncSession, country_code: str | None, plan: PlanTier
) -> dict[str, Feature]:
    """Every feature, resolved to the single row that governs it."""
    result = await session.execute(
        select(FeatureAvailability)
        .where(
            or_(
                FeatureAvailability.country_code.is_(None),
                FeatureAvailability.country_code == country_code,
            ),
            or_(
                FeatureAvailability.plan.is_(None),
                FeatureAvailability.plan == plan,
            ),
        )
        # Belt to uq_feature_scope's braces. That index is what actually stops
        # two rows of equal specificity existing; this makes the answer stable
        # regardless — without it "first seen" means "whatever order Postgres
        # returned", which changes with physical row order after a vacuum, so a
        # feature could silently flip on or off.
        .order_by(FeatureAvailability.id)
    )

    winner: dict[str, FeatureAvailability] = {}
    for row in result.scalars():
        current = winner.get(row.feature_key)
        # Strictly greater, so an equal-specificity row keeps the first seen —
        # which the ORDER BY above makes a fixed choice rather than a race.
        if current is None or _specificity(row) > _specificity(current):
            winner[row.feature_key] = row

    return {
        key: Feature(
            enabled=row.is_enabled, reason=None if row.is_enabled else row.reason
        )
        for key, row in winner.items()
    }


async def _plan_for(session: AsyncSession, household: Household) -> PlanTier:
    result = await session.execute(
        select(SubscriptionEntitlement)
        .where(
            SubscriptionEntitlement.household_id == household.id,
            SubscriptionEntitlement.is_active.is_(True),
        )
        .order_by(SubscriptionEntitlement.created_at.desc())
    )
    entitlement = result.scalars().first()
    return entitlement.plan if entitlement else PlanTier.free


async def resolve(session: AsyncSession, household: Household) -> Capabilities:
    """The resolved payload for one household.

    Never raises on an unknown region and never invents one: an unknown region
    is normal for the window between a social sign-in and the phone step, not an
    error state.
    """
    plan = await _plan_for(session, household)

    pack: CountryPack | None = None
    if household.country_code:
        result = await session.execute(
            select(CountryPack).where(
                CountryPack.country_code == household.country_code,
                # A pack can exist before its market opens — that is what
                # is_launched is for. Staging one must not start serving its
                # content: disclaimer_version points at legal copy that has not
                # been approved yet, and shipping unapproved disclaimer text is
                # the exact risk the flag guards (Appendix A).
                CountryPack.is_launched.is_(True),
            )
        )
        pack = result.scalar_one_or_none()

    features = await _feature_rows(session, household.country_code, plan)

    if pack is None:
        # No launched market — the region is unknown (NULL), unconfigured (no
        # pack row), or configured but not yet launched. All three mean the same
        # thing to a client: no market content to render.
        #
        # Deliberately does NOT force features off. An earlier version did, and
        # it disabled document_upload for every user: a household's region is
        # NULL until the phone step completes, so blanket region gating turned
        # off the core loop of v1 for everyone.
        #
        # The wildcard precedence already handles this correctly. A
        # market-specific feature is globally off with a country row enabling
        # it, so no matching pack means the global default applies. Overriding
        # on top of that was both wrong and redundant.
        return Capabilities(
            region=household.country_code,
            currency=UNKNOWN_REGION_CURRENCY,
            locale=UNKNOWN_REGION_LOCALE,
            features=features,
            content={},
            onboarding_required=(
                [ONBOARDING_PHONE] if household.country_code is None else []
            ),
        )

    return Capabilities(
        region=pack.country_code,
        currency=pack.currency,
        locale=pack.locale,
        features=features,
        content={
            "tax_accounts": pack.tax_accounts or [],
            "disclaimer_version": pack.disclaimer_version,
        },
    )


def require_feature(feature_key: str):
    """Refuse a request when the feature is off for this household.

    The payload hides it; this refuses it. Both are needed — a client is not a
    security boundary, and a token holder can call any endpoint directly.
    """

    async def dependency(
        household: Household = Depends(current_household),
        session: AsyncSession = Depends(get_session),
    ) -> None:
        capabilities = await resolve(session, household)
        feature = capabilities.features.get(feature_key)
        if feature is None or not feature.enabled:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "feature_unavailable",
                    "feature": feature_key,
                    "reason": feature.reason if feature else REASON_UNKNOWN_FEATURE,
                },
            )

    return dependency
