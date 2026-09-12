"""Region from the verified phone, the region override, one onboarding rule,
and signup consent (#24).

`current_user` is overridden as in test_me_endpoint.py. Every test rolls back.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.auth import AuthenticatedUser, current_user
from app.models.enums import PolicyKind, RegionSource
from app.models.identity import ConsentEvent, HouseholdRegionChange
from app.models.platform import DisclaimerVersion
from tests.conftest import requires_db

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

# +800 freephone: a real, valid number that belongs to no country.
UNPLACEABLE = "+80012345678"


def authenticate_as(*, sub=None, provider="phone", email=None, phone=None) -> str:
    from app.main import app

    sub = sub or str(uuid.uuid4())
    caller = AuthenticatedUser(
        user_id=sub,
        email=email,
        phone=phone,
        claims={"app_metadata": {"provider": provider}},
    )
    app.dependency_overrides[current_user] = lambda: caller
    return sub


async def _region_changes(session, household_id: str) -> list[HouseholdRegionChange]:
    result = await session.execute(
        select(HouseholdRegionChange).where(
            HouseholdRegionChange.household_id == uuid.UUID(household_id)
        )
    )
    return list(result.scalars())


class TestRegionFromPhone:
    async def test_a_phone_user_gets_their_region_on_the_first_call(
        self, api_client, db_session
    ):
        authenticate_as(phone="14165550100")  # the +-less form real tokens carry
        body = (await api_client.get("/me")).json()

        assert body["household"]["country_code"] == "CA"
        changes = await _region_changes(db_session, body["household"]["id"])
        assert [
            (c.previous_country_code, c.new_country_code, c.source) for c in changes
        ] == [(None, "CA", RegionSource.phone)]
        assert changes[0].changed_by_user_id == uuid.UUID(body["user"]["id"])

    async def test_a_google_user_gets_it_once_the_phone_is_verified(self, api_client):
        sub = authenticate_as(provider="google", email="r1@example.com")
        assert (await api_client.get("/me")).json()["household"]["country_code"] is None

        authenticate_as(
            sub=sub, provider="google", email="r1@example.com", phone="+12125550101"
        )
        assert (await api_client.get("/me")).json()["household"]["country_code"] == "US"

    async def test_a_later_phone_change_never_moves_the_region(
        self, api_client, db_session
    ):
        sub = authenticate_as(phone="+14165550102")
        before = (await api_client.get("/me")).json()

        authenticate_as(sub=sub, phone="+442071230103")
        after = (await api_client.get("/me")).json()

        assert after["user"]["phone"] == "+442071230103"
        assert after["household"]["country_code"] == "CA"
        assert len(await _region_changes(db_session, before["household"]["id"])) == 1

    async def test_repeat_calls_log_the_region_once(self, api_client, db_session):
        authenticate_as(phone="+14165550104")
        first = (await api_client.get("/me")).json()
        await api_client.get("/me")
        await api_client.get("/capabilities")
        assert len(await _region_changes(db_session, first["household"]["id"])) == 1

    async def test_a_number_that_cannot_be_placed_asks_for_a_region(self, api_client):
        authenticate_as(phone=UNPLACEABLE)
        body = (await api_client.get("/me")).json()

        assert body["household"]["country_code"] is None
        assert body["onboarding_required"] == ["region", "consent", "financial_setup"]

        fixed = (await api_client.put("/me/region", json={"country_code": "CA"})).json()
        assert fixed["household"]["country_code"] == "CA"
        assert fixed["onboarding_required"] == ["consent", "financial_setup"]


class TestRegionOverride:
    async def test_changes_the_region_and_audits_it(self, api_client, db_session):
        authenticate_as(phone="+14165550105")
        me = (await api_client.get("/me")).json()

        response = await api_client.put("/me/region", json={"country_code": "gb"})
        assert response.status_code == 200
        assert response.json()["household"]["country_code"] == "GB"

        changes = await _region_changes(db_session, me["household"]["id"])
        # created_at is transaction time, identical within one test — sort by source.
        logged = sorted(
            (c.source.value, c.previous_country_code, c.new_country_code)
            for c in changes
        )
        assert logged == [("phone", None, "CA"), ("user", "CA", "GB")]

    async def test_rejects_an_unknown_code(self, api_client):
        authenticate_as(phone="+14165550106")
        await api_client.get("/me")

        for code in ("ZZ", "CAN", "001", ""):
            response = await api_client.put("/me/region", json={"country_code": code})
            assert response.status_code == 422, code
            assert response.json()["detail"]["code"] == "unknown_region"

        assert (await api_client.get("/me")).json()["household"]["country_code"] == "CA"

    async def test_an_unchanged_region_writes_nothing(self, api_client, db_session):
        authenticate_as(phone="+14165550107")
        me = (await api_client.get("/me")).json()

        assert (
            await api_client.put("/me/region", json={"country_code": "CA"})
        ).status_code == 200
        assert len(await _region_changes(db_session, me["household"]["id"])) == 1

    async def test_any_known_country_is_accepted_launched_or_not(self, api_client):
        """Signup is never blocked on region; features are gated instead."""
        authenticate_as(phone="+14165550108")
        await api_client.get("/me")
        response = await api_client.put("/me/region", json={"country_code": "DE"})
        assert response.status_code == 200
        assert response.json()["household"]["country_code"] == "DE"


class TestOneOnboardingRule:
    async def test_me_and_capabilities_agree_in_every_state(self, api_client):
        async def both():
            me = (await api_client.get("/me")).json()["onboarding_required"]
            caps = (await api_client.get("/capabilities")).json()["onboarding_required"]
            return me, caps

        setup = ["financial_setup"]
        sub = authenticate_as(provider="google", email="o1@example.com")
        assert await both() == (["phone", "consent"] + setup,) * 2

        authenticate_as(
            sub=sub, provider="google", email="o1@example.com", phone=UNPLACEABLE
        )
        assert await both() == (["region", "consent"] + setup,) * 2

        await api_client.put("/me/region", json={"country_code": "CA"})
        assert await both() == (["consent"] + setup,) * 2

        version = (await api_client.get("/legal/terms")).json()["version"]
        await api_client.post("/me/consent", json={"version": version})
        # Consent done, but the figures are not: the wizard is the last step.
        assert await both() == (setup, setup)

        await api_client.put(
            "/financial-setup", json={"income": "4000", "monthly_expense": "1800"}
        )
        assert await both() == ([], [])


class TestConsent:
    async def test_the_terms_are_seeded_as_a_draft(self, api_client):
        authenticate_as(phone="+14165550109")
        terms = (await api_client.get("/legal/terms")).json()
        assert terms["version"] == "terms-v1"
        assert terms["body"].startswith("DRAFT")

    async def test_me_reports_the_terms_status(self, api_client):
        authenticate_as(phone="+14165550110")
        body = (await api_client.get("/me")).json()
        assert body["terms"] == {"version": "terms-v1", "accepted": False}

    async def test_consent_is_recorded_against_the_version_in_force(
        self, api_client, db_session
    ):
        authenticate_as(phone="+14165550111")
        me = (await api_client.get("/me")).json()

        response = await api_client.post("/me/consent", json={"version": "terms-v1"})
        assert response.status_code == 200
        body = response.json()
        assert body["terms"] == {"version": "terms-v1", "accepted": True}
        assert body["onboarding_required"] == ["financial_setup"]

        rows = (
            await db_session.execute(
                select(ConsentEvent, DisclaimerVersion.version)
                .join(
                    DisclaimerVersion,
                    DisclaimerVersion.id == ConsentEvent.disclaimer_version_id,
                )
                .where(ConsentEvent.user_id == uuid.UUID(me["user"]["id"]))
            )
        ).all()
        assert [version for _, version in rows] == ["terms-v1"]

    async def test_a_version_that_is_not_in_force_is_refused(
        self, api_client, db_session
    ):
        authenticate_as(phone="+14165550112")
        me = (await api_client.get("/me")).json()

        response = await api_client.post("/me/consent", json={"version": "terms-v0"})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "terms_version_mismatch"
        assert response.json()["detail"]["current_version"] == "terms-v1"

        count = await db_session.scalar(
            select(func.count())
            .select_from(ConsentEvent)
            .where(ConsentEvent.user_id == uuid.UUID(me["user"]["id"]))
        )
        assert count == 0

    async def test_accepting_twice_records_it_once(self, api_client, db_session):
        authenticate_as(phone="+14165550113")
        me = (await api_client.get("/me")).json()
        for _ in range(2):
            assert (
                await api_client.post("/me/consent", json={"version": "terms-v1"})
            ).status_code == 200

        count = await db_session.scalar(
            select(func.count())
            .select_from(ConsentEvent)
            .where(ConsentEvent.user_id == uuid.UUID(me["user"]["id"]))
        )
        assert count == 1


class TestConsentIsDedupedByTheDatabase:
    async def test_the_database_refuses_a_duplicate_consent(
        self, api_client, db_session
    ):
        """Not just the endpoint's check: two simultaneous accepts both pass that."""
        authenticate_as(phone="+14165550115")
        me = (await api_client.get("/me")).json()
        user_id = uuid.UUID(me["user"]["id"])
        terms = await db_session.scalar(
            select(DisclaimerVersion).where(DisclaimerVersion.version == "terms-v1")
        )

        db_session.add(ConsentEvent(user_id=user_id, disclaimer_version_id=terms.id))
        await db_session.flush()
        db_session.add(ConsentEvent(user_id=user_id, disclaimer_version_id=terms.id))
        with pytest.raises(IntegrityError):
            await db_session.flush()


class TestTermsInForce:
    async def _terms(self, session, version, effective_from):
        session.add(
            DisclaimerVersion(
                country_code=None,
                version=version,
                kind=PolicyKind.account_terms,
                body="test",
                effective_from=effective_from,
            )
        )
        await session.flush()

    async def test_an_undated_version_is_a_draft_not_the_terms(
        self, api_client, db_session
    ):
        await self._terms(db_session, "terms-draft", None)
        authenticate_as(phone="+14165550116")
        assert (await api_client.get("/legal/terms")).json()["version"] == "terms-v1"

    async def test_a_future_version_is_not_in_force_yet(self, api_client, db_session):
        await self._terms(db_session, "terms-future", func.now() + timedelta(days=30))
        authenticate_as(phone="+14165550117")
        assert (await api_client.get("/legal/terms")).json()["version"] == "terms-v1"

    async def test_a_newer_version_in_force_asks_for_consent_again(
        self, api_client, db_session
    ):
        authenticate_as(phone="+14165550118")
        await api_client.get("/me")
        await api_client.post("/me/consent", json={"version": "terms-v1"})

        await self._terms(db_session, "terms-v2", func.now())
        body = (await api_client.get("/me")).json()
        assert body["terms"] == {"version": "terms-v2", "accepted": False}
        assert body["onboarding_required"] == ["consent", "financial_setup"]


class TestSeeds:
    async def test_the_ca_pack_disclaimer_is_backed(self, db_session):
        row = (
            await db_session.execute(
                select(DisclaimerVersion).where(
                    DisclaimerVersion.country_code == "CA",
                    DisclaimerVersion.version == "ca-v1",
                )
            )
        ).scalar_one()
        assert row.kind == PolicyKind.regional_disclaimer
        assert row.body.startswith("DRAFT")
