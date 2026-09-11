"""Capability resolution — what a household may see and do.

Runs against a real Postgres: the resolution depends on seeded country packs
and on NULL-as-wildcard matching, neither of which SQLite reproduces. Each test
rolls back.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.enums import PlanTier
from app.models.identity import Household
from app.models.platform import (
    CountryPack,
    FeatureAvailability,
    SubscriptionEntitlement,
)
from app.services.capabilities import (
    UNKNOWN_REGION_CURRENCY,
    _plan_for,
    resolve,
)
from tests.conftest import requires_db

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]


async def _household(session, country_code: str | None) -> Household:
    household = Household(country_code=country_code)
    session.add(household)
    await session.flush()
    return household


async def _entitle(session, household, plan: PlanTier) -> None:
    session.add(
        SubscriptionEntitlement(household_id=household.id, plan=plan, is_active=True)
    )
    await session.flush()


async def _feature(
    session, key, *, country=None, plan=None, enabled=False, reason=None
):
    session.add(
        FeatureAvailability(
            feature_key=key,
            country_code=country,
            plan=plan,
            is_enabled=enabled,
            reason=reason,
        )
    )
    await session.flush()


class TestSeededCanada:
    async def test_returns_the_real_country_pack(self, db_session):
        household = await _household(db_session, "CA")
        result = await resolve(db_session, household)

        assert result.region == "CA"
        assert result.currency == "CAD"
        assert result.locale == "en-CA"
        assert result.content["tax_accounts"] == ["RRSP", "TFSA", "FHSA"]

    async def test_features_come_from_the_database(self, db_session):
        household = await _household(db_session, "CA")
        result = await resolve(db_session, household)
        # Seeded by the migration; absence would mean the client cannot tell
        # "off" from "unknown".
        assert "bank_linking" in result.features
        assert result.features["bank_linking"].enabled is False
        assert result.features["bank_linking"].reason == "coming_soon"

    async def test_an_enabled_feature_carries_no_reason(self, db_session):
        household = await _household(db_session, "CA")
        result = await resolve(db_session, household)
        assert result.features["document_upload"].enabled is True
        assert result.features["document_upload"].reason is None


class TestUnknownRegion:
    """Normal, not exceptional: the window before the phone step completes."""

    async def test_does_not_fail_and_does_not_guess(self, db_session):
        household = await _household(db_session, None)
        result = await resolve(db_session, household)

        assert result.region is None
        assert result.currency == UNKNOWN_REGION_CURRENCY

    async def test_region_independent_features_still_work(self, db_session):
        """The bug this replaced: forcing everything off disabled uploads.

        A household's region is NULL until the phone step completes, so blanket
        region gating turned off the core loop of v1 for every user.
        """
        household = await _household(db_session, None)
        result = await resolve(db_session, household)
        assert result.features["document_upload"].enabled is True

    async def test_a_region_specific_feature_stays_off(self, db_session):
        """Handled by precedence, not by an override.

        A market-specific feature is globally off with a country row enabling
        it; no country means the global default applies.
        """
        key = f"regional_{uuid.uuid4().hex[:8]}"
        household = await _household(db_session, None)
        await _feature(db_session, key, enabled=False, reason="region_unsupported")
        await _feature(db_session, key, country="CA", enabled=True)

        result = await resolve(db_session, household)
        assert result.features[key].enabled is False

    async def test_an_unconfigured_country_keeps_its_code_but_has_no_content(
        self, db_session
    ):
        """A country we have never configured — no pack row at all."""
        household = await _household(db_session, "ZZ")
        result = await resolve(db_session, household)

        assert result.region == "ZZ"
        assert result.onboarding_required == []
        assert result.content == {}


async def _staged_pack(session, country="DE") -> CountryPack:
    pack = CountryPack(
        country_code=country,
        currency="EUR",
        locale="de-DE",
        tax_accounts=["Riester"],
        disclaimer_version="de-v1",
        is_launched=False,
    )
    session.add(pack)
    await session.flush()
    return pack


class TestUnlaunchedMarket:
    """A pack can exist before its market opens; is_launched gates its content.

    Previously untested — the test that looked like this one used a country with
    no pack row, which is a different case. A staged pack was served as fully
    launched, disclaimer version and all.
    """

    async def test_a_staged_market_serves_no_content(self, db_session):
        """The disclaimer is the point: unapproved legal copy must not ship."""
        await _staged_pack(db_session)
        household = await _household(db_session, "DE")

        result = await resolve(db_session, household)

        assert result.region == "DE"
        assert result.content == {}

    async def test_it_still_serves_the_right_currency(self, db_session):
        """Withholding these would not leave the currency unknown — it would
        make it wrong. The pack already says EUR, and this is the only currency
        any client ever receives, so a fallback here misdenominates every amount
        in the app.
        """
        await _staged_pack(db_session)
        household = await _household(db_session, "DE")

        result = await resolve(db_session, household)

        assert result.currency == "EUR"
        assert result.locale == "de-DE"
        assert result.currency != UNKNOWN_REGION_CURRENCY

    async def test_a_market_specific_feature_still_applies(self, db_session):
        """Features and packs are separate axes.

        A staged market is exactly where you would switch a feature on to test
        it, so the country code drives feature rows regardless of launch state.
        """
        key = f"staged_{uuid.uuid4().hex[:8]}"
        await _staged_pack(db_session)
        await _feature(db_session, key, enabled=False, reason="coming_soon")
        await _feature(db_session, key, country="DE", enabled=True)

        household = await _household(db_session, "DE")
        result = await resolve(db_session, household)

        assert result.features[key].enabled is True

    async def test_launching_it_is_one_column(self, db_session):
        """Flipping the flag opens the market; nothing else changes."""
        pack = await _staged_pack(db_session)
        household = await _household(db_session, "DE")
        before = await resolve(db_session, household)
        assert before.content == {}

        pack.is_launched = True
        await db_session.flush()

        after = await resolve(db_session, household)
        assert after.content["disclaimer_version"] == "de-v1"
        assert after.content["tax_accounts"] == ["Riester"]
        # Unchanged by launching — they were already correct.
        assert after.currency == before.currency == "EUR"
        assert after.locale == before.locale == "de-DE"


class TestPrecedence:
    """Most specific wins: country+plan > country > plan > global."""

    async def test_a_country_row_overrides_the_global_default(self, db_session):
        key = f"prec_{uuid.uuid4().hex[:8]}"
        household = await _household(db_session, "CA")
        await _feature(db_session, key, enabled=False, reason="coming_soon")
        await _feature(db_session, key, country="CA", enabled=True)

        result = await resolve(db_session, household)
        assert result.features[key].enabled is True

    async def test_country_outranks_plan(self, db_session):
        """A paid plan must not unlock what the country does not offer."""
        key = f"prec_{uuid.uuid4().hex[:8]}"
        household = await _household(db_session, "CA")
        await _entitle(db_session, household, PlanTier.personal)
        await _feature(
            db_session, key, country="CA", enabled=False, reason="region_unsupported"
        )
        await _feature(db_session, key, plan=PlanTier.personal, enabled=True)

        result = await resolve(db_session, household)
        assert result.features[key].enabled is False

    async def test_the_exact_row_beats_both(self, db_session):
        key = f"prec_{uuid.uuid4().hex[:8]}"
        household = await _household(db_session, "CA")
        await _entitle(db_session, household, PlanTier.personal)
        await _feature(
            db_session, key, country="CA", enabled=False, reason="coming_soon"
        )
        await _feature(
            db_session, key, country="CA", plan=PlanTier.personal, enabled=True
        )

        result = await resolve(db_session, household)
        assert result.features[key].enabled is True


class TestPlanGating:
    async def test_a_paid_feature_is_off_on_free(self, db_session):
        key = f"plan_{uuid.uuid4().hex[:8]}"
        household = await _household(db_session, "CA")
        await _feature(db_session, key, enabled=False, reason="not_in_plan")
        await _feature(db_session, key, plan=PlanTier.personal, enabled=True)

        result = await resolve(db_session, household)
        assert result.features[key].enabled is False
        assert result.features[key].reason == "not_in_plan"

    async def test_the_same_feature_is_on_for_paid(self, db_session):
        key = f"plan_{uuid.uuid4().hex[:8]}"
        household = await _household(db_session, "CA")
        await _entitle(db_session, household, PlanTier.personal)
        await _feature(db_session, key, enabled=False, reason="not_in_plan")
        await _feature(db_session, key, plan=PlanTier.personal, enabled=True)

        result = await resolve(db_session, household)
        assert result.features[key].enabled is True

    async def test_a_household_with_no_entitlement_is_free(self, db_session):
        household = await _household(db_session, "CA")
        key = f"plan_{uuid.uuid4().hex[:8]}"
        await _feature(db_session, key, plan=PlanTier.free, enabled=True)
        result = await resolve(db_session, household)
        assert result.features[key].enabled is True


class TestAddingACountryIsDataOnly:
    async def test_a_new_market_needs_no_code_change(self, db_session):
        """The criterion that keeps market differences out of the codebase."""
        db_session.add(
            CountryPack(
                country_code="GB",
                currency="GBP",
                locale="en-GB",
                tax_accounts=["ISA", "SIPP"],
                disclaimer_version="gb-v1",
                is_launched=True,
            )
        )
        await db_session.flush()

        household = await _household(db_session, "GB")
        result = await resolve(db_session, household)

        assert result.region == "GB"
        assert result.currency == "GBP"
        assert result.locale == "en-GB"
        assert result.content["tax_accounts"] == ["ISA", "SIPP"]


class TestScopeUniqueness:
    """Two rows of equal specificity must be impossible.

    Regression for c3a91e7d4b28. The original index covered the bare columns,
    and NULL means "any" here — so Postgres, treating NULLs as distinct, only
    ever constrained the country+plan scope. The other three accepted a second,
    contradicting row, and resolution then depended on physical row order: the
    same two rows resolved differently after a vacuum moved one.
    """

    @pytest.mark.parametrize(
        ("country", "plan"),
        [
            (None, None),  # global
            ("CA", None),  # this market, any plan
            (None, PlanTier.free),  # any market, this plan
            ("CA", PlanTier.free),  # exact — the only scope covered before
        ],
        ids=["global", "country_only", "plan_only", "country_and_plan"],
    )
    async def test_a_duplicate_scope_is_rejected(self, db_session, country, plan):
        key = f"dupe_{uuid.uuid4().hex[:8]}"
        await _feature(db_session, key, country=country, plan=plan, enabled=True)

        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await _feature(
                    db_session, key, country=country, plan=plan, enabled=False
                )

    async def test_different_scopes_still_coexist(self, db_session):
        """The index must not over-constrain — precedence needs all four rows.

        The household is entitled to `personal` so every row actually competes;
        on `free` the two plan-scoped rows would not match the query at all, and
        the test would pass without proving anything about them.
        """
        key = f"scopes_{uuid.uuid4().hex[:8]}"
        household = await _household(db_session, "CA")
        await _entitle(db_session, household, PlanTier.personal)

        await _feature(db_session, key, enabled=True)
        await _feature(db_session, key, country="CA", enabled=True)
        await _feature(db_session, key, plan=PlanTier.personal, enabled=True)
        # The most specific row, and the only one that disagrees — so it winning
        # is visible rather than coincidental.
        await _feature(
            db_session, key, country="CA", plan=PlanTier.personal, enabled=False
        )

        result = await resolve(db_session, household)
        assert result.features[key].enabled is False


class TestOneActiveEntitlement:
    """A household's plan must not depend on insertion order.

    Regression for d47f2b91c6ae. The model docstring always claimed one active
    row per household, but ix_entitlement_household_active is a plain lookup
    index — two active rows were accepted. _plan_for tie-breaks on
    created_at.desc(), which cannot discriminate between them: created_at
    defaults to now(), and Postgres now() is transaction start time, so rows
    written in one transaction carry identical timestamps. The plan was decided
    by whichever row happened to come back first.
    """

    async def test_a_second_active_row_is_rejected(self, db_session):
        household = await _household(db_session, "CA")
        await _entitle(db_session, household, PlanTier.personal)

        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await _entitle(db_session, household, PlanTier.family)

    async def test_history_may_keep_many_inactive_rows(self, db_session):
        """The index is partial for this reason — superseded rows accumulate."""
        household = await _household(db_session, "CA")
        for plan in (PlanTier.free, PlanTier.personal, PlanTier.family):
            db_session.add(
                SubscriptionEntitlement(
                    household_id=household.id, plan=plan, is_active=False
                )
            )
        await db_session.flush()
        await _entitle(db_session, household, PlanTier.personal)

        # Three inactive plus one active resolve unambiguously.
        assert (await resolve(db_session, household)).features is not None
        assert await _plan_for(db_session, household) == PlanTier.personal
