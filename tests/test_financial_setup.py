"""The financial setup wizard's persistence (#25) and its gate (#29).

Every test rolls back. `current_user` is overridden as elsewhere; each test
takes its caller through onboarding first, because these endpoints refuse while
a *prerequisite* step is outstanding.

They do not refuse for `financial_setup` itself — saving here is how that step
is cleared — so `onboard` leaves exactly that one step outstanding.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.auth import AuthenticatedUser, current_user
from app.models.planning import Debt
from app.models.setup import FinancialProfile
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
    """A caller past every prerequisite: phone, region, accepted terms.

    `financial_setup` is deliberately still outstanding — that is the state the
    wizard has to be usable in.
    """
    authenticate_as(phone=phone)
    me = (await api_client.get("/me")).json()
    version = (await api_client.get("/legal/terms")).json()["version"]
    me = (await api_client.post("/me/consent", json={"version": version})).json()
    assert me["onboarding_required"] == ["financial_setup"], me["onboarding_required"]
    return me


MANDATORY = {"income": "4000", "monthly_expense": "1800"}


async def steps(api_client) -> list[str]:
    return (await api_client.get("/me")).json()["onboarding_required"]


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


class TestOrdering:
    """The order the user typed, kept.

    Rows are rewritten on every save and share one `created_at` (Postgres now()
    is transaction time), so ordering on that alone came back alphabetical — the
    list reshuffled under the user between steps.
    """

    async def test_each_list_comes_back_in_the_order_it_was_sent(self, api_client):
        await onboard(api_client, "+14165560015")
        debts = ["Visa", "Car loan", "Student loan"]  # deliberately not alphabetical
        investments = ["TFSA", "RRSP", "FHSA"]
        obligations = ["Rent", "Phone", "Insurance"]
        body = {
            "debts": [a_debt(n) for n in debts],
            "investments": [{"name": n, "amount": "100"} for n in investments],
            "obligations": [{"name": n, "monthly_amount": "50"} for n in obligations],
        }
        saved = (await api_client.put(SETUP, json=body)).json()

        assert [d["name"] for d in saved["debts"]] == debts
        assert [i["name"] for i in saved["investments"]] == investments
        assert [o["name"] for o in saved["obligations"]] == obligations
        # And on the way back out, not just in the save's own response.
        assert (await api_client.get(SETUP)).json() == saved

    async def test_reordering_is_saved(self, api_client):
        await onboard(api_client, "+14165560016")
        await api_client.put(
            SETUP, json={"debts": [a_debt("Visa"), a_debt("Car loan")]}
        )
        reordered = (
            await api_client.put(
                SETUP, json={"debts": [a_debt("Car loan"), a_debt("Visa")]}
            )
        ).json()
        assert [d["name"] for d in reordered["debts"]] == ["Car loan", "Visa"]


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


class TestMonthlyExpense:
    """The second mandatory figure (#29), held to the same money rules."""

    async def test_round_trips_exactly(self, api_client):
        await onboard(api_client, "+14165560009")
        saved = (await api_client.put(SETUP, json=MANDATORY)).json()
        assert saved["monthly_expense"] == "1800.00"
        assert (await api_client.get(SETUP)).json()["monthly_expense"] == "1800.00"

    async def test_is_stored_as_minor_units(self, api_client, db_session):
        me = await onboard(api_client, "+14165560010")
        await api_client.put(SETUP, json=MANDATORY)

        units = (
            await db_session.execute(
                select(FinancialProfile.monthly_expense_minor_units).where(
                    FinancialProfile.household_id == uuid.UUID(me["household"]["id"])
                )
            )
        ).scalar_one()
        assert units == 180000

    async def test_a_value_that_is_not_money_is_refused_by_name(self, api_client):
        await onboard(api_client, "+14165560011")
        response = await api_client.put(
            SETUP, json={"income": "4000", "monthly_expense": "not money"}
        )
        assert response.status_code == 422
        assert response.json()["detail"]["field"] == "monthly_expense"

    async def test_a_negative_value_is_refused(self, api_client):
        await onboard(api_client, "+14165560012")
        response = await api_client.put(
            SETUP, json={"income": "4000", "monthly_expense": "-5"}
        )
        assert response.status_code == 422
        assert response.json()["detail"]["field"] == "monthly_expense"

    async def test_absent_until_it_is_answered(self, api_client):
        await onboard(api_client, "+14165560013")
        assert (await api_client.get(SETUP)).json()["monthly_expense"] is None


class TestFinancialSetupIsAnOnboardingStep:
    """The gate itself: both figures, or the app stays out of reach."""

    async def test_both_figures_clear_it(self, api_client):
        await onboard(api_client, "+14165560014")
        assert await steps(api_client) == ["financial_setup"]

        await api_client.put(SETUP, json=MANDATORY)
        assert await steps(api_client) == []

    async def test_income_alone_is_not_enough(self, api_client):
        await onboard(api_client, "+14165560015")
        await api_client.put(SETUP, json={"income": "4000"})
        assert await steps(api_client) == ["financial_setup"]

    async def test_expense_alone_is_not_enough(self, api_client):
        await onboard(api_client, "+14165560016")
        await api_client.put(SETUP, json={"monthly_expense": "1800"})
        assert await steps(api_client) == ["financial_setup"]

    async def test_a_zero_figure_still_counts_as_answered(self, api_client):
        """Nothing earned and nothing spent is a real answer, not a blank."""
        await onboard(api_client, "+14165560017")
        await api_client.put(SETUP, json={"income": "0", "monthly_expense": "0"})
        assert await steps(api_client) == []

    async def test_clearing_a_figure_raises_the_gate_again(self, api_client):
        """A save replaces what the wizard owns, so omitting income clears it."""
        await onboard(api_client, "+14165560018")
        await api_client.put(SETUP, json=MANDATORY)
        assert await steps(api_client) == []

        await api_client.put(SETUP, json={"monthly_expense": "1800"})
        assert await steps(api_client) == ["financial_setup"]


class TestTheWizardStaysReachable:
    """The circularity this ticket exists to avoid.

    `financial_setup` is an onboarding step and the wizard is how it is cleared,
    so the wizard must never be gated on it — or the user is locked out of the
    only endpoint that can let them in.
    """

    async def test_it_works_while_financial_setup_is_the_only_step_left(
        self, api_client
    ):
        await onboard(api_client, "+14165560019")
        assert await steps(api_client) == ["financial_setup"]

        assert (await api_client.get(SETUP)).status_code == 200
        assert (await api_client.put(SETUP, json=MANDATORY)).status_code == 200

    async def test_it_still_refuses_while_a_prerequisite_is_outstanding(
        self, api_client
    ):
        """The currency is not known until the region is."""
        authenticate_as(provider="google", email="setup1@example.com")
        await api_client.get("/me")

        for call in (
            api_client.get(SETUP),
            api_client.put(SETUP, json={"income": "1"}),
        ):
            response = await call
            assert response.status_code == 409
            detail = response.json()["detail"]
            assert detail["code"] == "onboarding_required"
            assert "phone" in detail["onboarding_required"]
            # Never the step this endpoint exists to clear.
            assert "financial_setup" not in detail["onboarding_required"]


class TestRequireOnboarded:
    """Enforcement for every OTHER endpoint, proven before one needs it.

    Mounted on a throwaway route exactly as `require_feature` is, so the pattern
    is established rather than retrofitted once endpoints already exist.
    """

    def _mount(self) -> str:
        from fastapi import Depends

        from app.main import app
        from app.services.onboarding import require_onboarded

        path = f"/__test__/onboarded-{uuid.uuid4().hex[:6]}"

        @app.get(path, dependencies=[Depends(require_onboarded)])
        async def _guarded() -> dict[str, bool]:
            return {"reached": True}

        return path

    async def test_refuses_while_financial_setup_is_outstanding(self, api_client):
        await onboard(api_client, "+14165560020")

        response = await api_client.get(self._mount())
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["code"] == "onboarding_required"
        assert detail["onboarding_required"] == ["financial_setup"]

    async def test_passes_once_the_figures_are_saved(self, api_client):
        await onboard(api_client, "+14165560021")
        await api_client.put(SETUP, json=MANDATORY)

        response = await api_client.get(self._mount())
        assert response.status_code == 200
        assert response.json() == {"reached": True}


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
