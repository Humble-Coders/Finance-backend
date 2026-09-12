"""The financial setup wizard's persistence (#25).

Every test rolls back. `current_user` is overridden as elsewhere; each test
takes its caller through onboarding first, because these endpoints refuse until
that is complete.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.auth import AuthenticatedUser, current_user
from app.models.planning import Debt
from tests.conftest import requires_db

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

SETUP = "/financial-setup"


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


async def onboard(api_client, phone: str) -> dict:
    """A caller who has finished onboarding: phone, region, accepted terms."""
    authenticate_as(phone=phone)
    me = (await api_client.get("/me")).json()
    version = (await api_client.get("/legal/terms")).json()["version"]
    me = (await api_client.post("/me/consent", json={"version": version})).json()
    assert me["onboarding_required"] == [], me["onboarding_required"]
    return me


def a_debt(name="Visa", balance="2500.00", minimum="75", rate="19.99") -> dict:
    return {
        "name": name,
        "balance": balance,
        "minimum_payment": minimum,
        "interest_rate_percent": rate,
    }


class TestRoundTrip:
    async def test_saves_and_returns_every_part_exactly(self, api_client):
        await onboard(api_client, "+14165560001")

        body = {
            "income": "4200",
            "debts": [a_debt()],
            "investments": [{"name": "TFSA", "amount": "15000.25"}],
            "obligations": [{"name": "Rent", "monthly_amount": "1800"}],
        }
        saved = (await api_client.put(SETUP, json=body)).json()

        assert saved["currency"] == "CAD"
        assert saved["income"] == "4200.00"
        assert saved["debts"] == [
            {
                "name": "Visa",
                "balance": "2500.00",
                "minimum_payment": "75.00",
                "interest_rate_percent": "19.99",
            }
        ]
        assert saved["investments"] == [{"name": "TFSA", "amount": "15000.25"}]
        assert saved["obligations"] == [{"name": "Rent", "monthly_amount": "1800.00"}]
        assert (await api_client.get(SETUP)).json() == saved

    async def test_stores_integer_minor_units(self, api_client, db_session):
        me = await onboard(api_client, "+14165560002")
        await api_client.put(SETUP, json={"debts": [a_debt(balance="1200")]})

        debt = (
            await db_session.execute(
                select(Debt).where(
                    Debt.household_id == uuid.UUID(me["household"]["id"])
                )
            )
        ).scalar_one()
        assert debt.balance_minor_units == 120000
        assert debt.interest_rate_bps == 1999
        assert debt.entered_via_setup is True

    async def test_an_empty_wizard_is_allowed(self, api_client):
        """Every step is skippable, so an empty save is valid."""
        await onboard(api_client, "+14165560003")
        saved = (await api_client.put(SETUP, json={})).json()
        assert saved["income"] is None
        assert saved["debts"] == saved["investments"] == saved["obligations"] == []


class TestValidation:
    @pytest.mark.parametrize(
        ("body", "field"),
        [
            ({"income": "-5"}, "income"),
            ({"income": "12.345"}, "income"),
            ({"income": "abc"}, "income"),
            ({"debts": [a_debt(balance="-1")]}, "debts.0.balance"),
            ({"debts": [a_debt(rate="101")]}, "debts.0.interest_rate_percent"),
            ({"debts": [a_debt(rate="5.255")]}, "debts.0.interest_rate_percent"),
            (
                {"investments": [{"name": "x", "amount": "1.234"}]},
                "investments.0.amount",
            ),
            (
                {"obligations": [{"name": "x", "monthly_amount": "-0.01"}]},
                "obligations.0.monthly_amount",
            ),
        ],
    )
    async def test_rejects_bad_amounts_naming_the_field(self, api_client, body, field):
        await onboard(api_client, f"+1416556{uuid.uuid4().int % 10000:04d}")
        response = await api_client.put(SETUP, json=body)
        assert response.status_code == 422
        assert response.json()["detail"]["field"] == field

    async def test_rejects_an_oversized_list(self, api_client):
        await onboard(api_client, "+14165560004")
        many = [{"name": f"Item {i}", "amount": "1"} for i in range(21)]
        assert (
            await api_client.put(SETUP, json={"investments": many})
        ).status_code == 422

    async def test_rejects_an_empty_name(self, api_client):
        await onboard(api_client, "+14165560005")
        body = {"obligations": [{"name": "", "monthly_amount": "10"}]}
        assert (await api_client.put(SETUP, json=body)).status_code == 422


class TestReplaceSemantics:
    async def test_two_identical_saves_leave_identical_data(self, api_client):
        await onboard(api_client, "+14165560006")
        body = {
            "income": "100",
            "debts": [a_debt()],
            "obligations": [{"name": "Rent", "monthly_amount": "1800"}],
        }
        first = (await api_client.put(SETUP, json=body)).json()
        second = (await api_client.put(SETUP, json=body)).json()
        assert first == second

    async def test_a_save_with_fewer_debts_drops_the_rest(self, api_client):
        await onboard(api_client, "+14165560007")
        await api_client.put(
            SETUP, json={"debts": [a_debt("Visa"), a_debt("Car loan", "9000")]}
        )
        saved = (await api_client.put(SETUP, json={"debts": [a_debt("Visa")]})).json()
        assert [d["name"] for d in saved["debts"]] == ["Visa"]

    async def test_never_touches_a_debt_it_does_not_own(self, api_client, db_session):
        """Debts from statements (M3) must survive a wizard save."""
        me = await onboard(api_client, "+14165560008")
        household_id = uuid.UUID(me["household"]["id"])
        db_session.add(
            Debt(
                household_id=household_id,
                name="From a statement",
                balance_minor_units=5000,
                currency="CAD",
                entered_via_setup=False,
            )
        )
        await db_session.flush()

        await api_client.put(SETUP, json={"debts": [a_debt()]})
        await api_client.put(SETUP, json={"debts": []})

        names = (
            (
                await db_session.execute(
                    select(Debt.name).where(Debt.household_id == household_id)
                )
            )
            .scalars()
            .all()
        )
        assert names == ["From a statement"]


class TestStatus:
    async def test_starts_not_started(self, api_client):
        await onboard(api_client, "+14165560009")
        assert (await api_client.get(SETUP)).json()["status"] == "not_started"

    async def test_finishing_completes_it(self, api_client):
        await onboard(api_client, "+14165560010")
        saved = (
            await api_client.put(SETUP, json={"income": "1", "finished": True})
        ).json()
        assert saved["status"] == "completed"
        assert (await api_client.get(SETUP)).json()["status"] == "completed"

    async def test_skipping_keeps_what_was_saved(self, api_client):
        await onboard(api_client, "+14165560011")
        await api_client.put(SETUP, json={"income": "3000"})
        skipped = (await api_client.post(f"{SETUP}/skip")).json()
        assert skipped["status"] == "skipped"
        assert skipped["income"] == "3000.00"

    async def test_a_later_save_does_not_un_complete_it(self, api_client):
        await onboard(api_client, "+14165560012")
        await api_client.put(SETUP, json={"finished": True})
        after = (await api_client.put(SETUP, json={"income": "50"})).json()
        assert after["status"] == "completed"


class TestOnboardingGate:
    async def test_all_three_endpoints_refuse_until_onboarding_is_done(
        self, api_client
    ):
        """The currency is not known until the region is."""
        authenticate_as(provider="google", email="setup1@example.com")
        await api_client.get("/me")

        for call in (
            api_client.get(SETUP),
            api_client.put(SETUP, json={"income": "1"}),
            api_client.post(f"{SETUP}/skip"),
        ):
            response = await call
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "onboarding_required"
            assert "phone" in response.json()["detail"]["onboarding_required"]


class TestHouseholdIsolation:
    async def test_one_household_never_sees_another(self, api_client):
        await onboard(api_client, "+14165560013")
        await api_client.put(
            SETUP,
            json={
                "income": "9999",
                "obligations": [{"name": "Rent", "monthly_amount": "1"}],
            },
        )

        await onboard(api_client, "+14165560014")
        theirs = (await api_client.get(SETUP)).json()
        assert theirs["income"] is None
        assert theirs["obligations"] == []
