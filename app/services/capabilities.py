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

# Used when no country pack is available to say otherwise, which is two cases:
# the region is still NULL (the window between a Google/Apple sign-in and the
# phone step), or we hold a country code we have never configured. In both we
# genuinely do not know the currency.
#
# A pack that exists but is not launched is NOT one of these — its currency and
# locale are configured and correct, so they are served from the pack. Only the
# publishable content is withheld. See `_content`.
#
# Deliberately a documented default rather than a guess: guessing a country
# shows someone the wrong tax accounts and the wrong disclaimer (PRD §4.6).
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


def _content(pack: CountryPack) -> dict[str, object]:
    """The publishable half of a country pack — empty until the market opens.

    `is_launched` gates content, not the whole pack. `disclaimer_version` points
    at legal copy that has to be approved before anyone sees it, and tax account
    names are educational content for a market we have not opened; shipping
    either from a staged pack is the risk the flag exists to prevent
    (Appendix A). Currency and locale are facts rather than published content,
    so they are served regardless — see `resolve`.
    """
    if not pack.is_launched:
        return {}
    return {
        "tax_accounts": pack.tax_accounts or [],
        "disclaimer_version": pack.disclaimer_version,
    }


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
                CountryPack.country_code == household.country_code
            )
        )
        pack = result.scalar_one_or_none()

    # Note the country code, not the pack, decides which feature rows apply — so
    # a market-specific feature row takes effect in a market that is configured
    # but not yet launched. That is intended: features and packs are separate
    # axes, and a staged market is exactly where you would switch one on to test
    # it. Covered by TestUnlaunchedMarket.
    features = await _feature_rows(session, household.country_code, plan)

    if pack is None:
        # No pack at all — the region is either unknown (NULL, the window before
        # the phone step) or a country code we have never configured. Either way
        # there is no currency to serve but the documented default.
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

    # Currency and locale come from the pack whether or not the market is open:
    # they are facts about it, and they are already configured. Substituting the
    # fallback here would not leave the currency unknown, it would make it wrong
    # — a German user's spending rendered in Canadian dollars — and this is the
    # only currency any client ever receives.
    return Capabilities(
        region=pack.country_code,
        currency=pack.currency,
        locale=pack.locale,
        features=features,
        content=_content(pack),
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
