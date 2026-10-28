"""`/plan` and `/plan/preferences` (#76), and the counting the account links make
possible: the score's debt payments (formula v4) and the budget's Debt line."""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.models.planning import Debt
from app.services import budget as budget_service
from app.services.debts import covered_minimums
from tests.conftest import requires_db
from tests.test_budgets_api import a_ready_household
from tests.test_dashboard import a_household, a_system_category_id, tx

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

TODAY = date(2026, 9, 15)
PREFS = "/plan/preferences"


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    for module in ("plan", "budgets"):
        monkeypatch.setattr(f"app.api.{module}._today", lambda: TODAY)


def cents(amount: str | None) -> int:
    whole, part = (amount or "0.00").split(".")
    return int(whole) * 100 + int(part)


async def a_rate_on_the_visa(api_client) -> str:
    """The ready household's setup Visa, with a rate so it can be planned."""
    visa = (await api_client.get("/debts")).json()["debts"][0]
    edited = await api_client.patch(
        f"/debts/{visa['id']}", json={"interest_rate_percent": "20.99"}
    )
    assert edited.status_code == 200, edited.text
    return visa["id"]


class TestPreferences:
    async def test_until_chosen_the_plan_runs_on_suggestions(
        self, api_client, db_session
    ):
        await a_ready_household(api_client, db_session, "+14165579101")

        body = (await api_client.get(PREFS)).json()

        assert body["saved"] is False
        suggested = body["suggested"]
        assert (suggested["goal_share_percent"], suggested["rate_line_percent"]) == (
            "20.00",
            "6.00",
        )
        assert suggested["strategy"] == "avalanche"
        assert suggested["buffer"] is not None and suggested["buffer"].endswith(
            ".00"
        ), "one month of spending, rounded up to a whole dollar"
        assert body["buffer"] is None, "the plan has no buffer step"

    async def test_a_buffer_from_an_older_app_is_ignored(self, api_client, db_session):
        """Your plan has no buffer step (2026-10-28): a buffer sent by an app
        from before is kept nowhere, and no Emergency fund goal is made."""
        await a_ready_household(api_client, db_session, "+14165579102")

        saved = await api_client.put(
            PREFS,
            json={
                "buffer": "1500.00",
                "goal_share_percent": "25",
                "rate_line_percent": "7",
                "strategy": "snowball",
            },
        )

        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert (body["saved"], body["buffer"], body["buffer_goal_id"]) == (
            True,
            None,
            None,
        )
        assert (body["goal_share_percent"], body["rate_line_percent"]) == (
            "25.00",
            "7.00",
        )
        goals = (await api_client.get("/goals")).json()["goals"]
        assert goals == []

    async def test_an_emergency_fund_goal_is_a_goal_like_any_other(
        self, api_client, db_session
    ):
        await a_ready_household(api_client, db_session, "+14165579103")
        made = await api_client.post(
            "/goals",
            json={
                "name": "Rainy day",
                "kind": "emergency_fund",
                "horizon": "short_term",
                "target": "800.00",
            },
        )
        assert made.status_code == 201, made.text

        plan = (await api_client.get("/plan")).json()

        assert plan["split"]["buffer"] in (None, "0.00")
        assert made.json()["id"] in {g["goal_id"] for g in plan["split"]["goals"]}

    @pytest.mark.parametrize(
        ("phone", "change", "field"),
        [
            ("+14165579111", {"goal_share_percent": "120"}, "goal_share_percent"),
            ("+14165579112", {"rate_line_percent": "abc"}, "rate_line_percent"),
            ("+14165579113", {"buffer": "0"}, "buffer"),
            ("+14165579114", {"buffer": "10.555"}, "buffer"),
        ],
    )
    async def test_a_value_it_cannot_keep_is_refused_by_name(
        self, api_client, phone, change, field
    ):
        await a_household(api_client, phone)
        choice = {
            "buffer": "1000.00",
            "goal_share_percent": "20",
            "rate_line_percent": "6",
            "strategy": "avalanche",
        }

        response = await api_client.put(PREFS, json={**choice, **change})

        assert response.status_code == 422, response.text
        assert response.json()["detail"]["field"] == field


class TestThePlan:
    async def test_the_money_left_over_is_the_budgets_savings_and_splits_exactly(
        self, api_client, db_session
    ):
        await a_ready_household(api_client, db_session, "+14165579120")
        visa = await a_rate_on_the_visa(api_client)

        plan = (await api_client.get("/plan", params={"month": "2026-09"})).json()
        budget = (await api_client.get("/budgets/2026-09")).json()

        assert plan["status"] == "ready"
        # What income leaves after every line but savings.
        assert plan["leftover"] == budget["savings_room"]
        split = plan["split"]
        parts = cents(split["buffer"]) + sum(cents(g["amount"]) for g in split["goals"])
        parts += cents(split["extra_debt"]) + cents(split["save_or_invest"])
        assert parts == cents(plan["leftover"]), "every dollar accounted for"
        debt = next(d for d in plan["debts"] if d["debt_id"] == visa)
        assert debt["rate_percent"] == "20.99"
        assert debt["missing_rate"] is False
        assert plan["totals"]["other_strategy"] == "snowball"

    async def test_a_shortfall_leaves_nothing_to_split(self, api_client, db_session):
        _, _, ids = await a_ready_household(api_client, db_session, "+14165579124")
        await a_rate_on_the_visa(api_client)
        over = await api_client.put(
            f"/budgets/2026-09/lines/{ids['groceries']}", json={"amount": "6000.00"}
        )
        assert over.json()["shortfall"] is not None, over.text

        plan = (await api_client.get("/plan", params={"month": "2026-09"})).json()

        assert plan["shortfall"] == over.json()["shortfall"]
        assert plan["leftover"] == "0.00"
        split = plan["split"]
        assert (split["buffer"], split["extra_debt"], split["save_or_invest"]) == (
            "0.00",
            "0.00",
            "0.00",
        )
        assert split["goals"] == []

    async def test_a_debt_without_a_rate_is_named_not_planned(
        self, api_client, db_session
    ):
        await a_ready_household(api_client, db_session, "+14165579121")

        plan = (await api_client.get("/plan", params={"month": "2026-09"})).json()

        visa = plan["debts"][0]
        assert visa["missing_rate"] is True
        assert visa["paid_off"] is None
        assert plan["totals"]["debt_free"] is None

    async def test_still_learning_has_nothing_to_split(self, api_client):
        await a_household(api_client, "+14165579122")
        await api_client.post(
            "/debts",
            json={
                "name": "Visa",
                "balance": "1000.00",
                "minimum_payment": "50.00",
                "interest_rate_percent": "19.99",
            },
        )

        plan = (await api_client.get("/plan")).json()

        assert plan["status"] == "learning"
        assert plan["leftover"] == "0.00"
        assert plan["debts"][0]["paid_off"] is not None, "the minimums still pay it off"

    async def test_a_month_it_cannot_read_is_a_422(self, api_client):
        await a_household(api_client, "+14165579123")

        assert (
            await api_client.get("/plan", params={"month": "2026-13"})
        ).status_code == 422


class TestDebtPaymentsByDebt:
    """Score v4: a payment counts toward the debt it reached."""

    async def household_with_a_card_and_a_loan(
        self, api_client, db_session, phone, *, link
    ):
        household, chequing = await a_household(api_client, phone)
        card = (
            await api_client.post(
                "/accounts", json={"name": "Visa card", "kind": "credit_card"}
            )
        ).json()["id"]
        db_session.add_all(
            [
                Debt(
                    household_id=household,
                    name="Visa",
                    balance_minor_units=0,
                    minimum_payment_minor_units=10_000,
                    currency="CAD",
                    account_id=uuid.UUID(card) if link else None,
                ),
                Debt(
                    household_id=household,
                    name="Loan",
                    balance_minor_units=500_000,
                    minimum_payment_minor_units=30_000,
                    currency="CAD",
                ),
            ]
        )
        transfers = await a_system_category_id(db_session, "transfers")
        # August: the card paid off in full ($1,000 arrives on it); no loan payment.
        db_session.add(
            tx(
                household,
                uuid.UUID(card),
                minor=100_000,
                month=8,
                day=20,
                credit=True,
                category_id=transfers,
                description="PAYMENT THANK YOU",
            )
        )
        await db_session.commit()
        return household

    async def test_a_card_paid_in_full_no_longer_covers_a_missed_loan(
        self, api_client, db_session
    ):
        household = await self.household_with_a_card_and_a_loan(
            api_client, db_session, "+14165579130", link=True
        )

        covered = await covered_minimums(
            db_session, household, "CAD", date(2026, 8, 1), 100_000
        )

        assert (
            covered == 10_000
        ), "the Visa's $100 minimum, and nothing toward the loan's $300"

    async def test_with_nothing_linked_it_is_the_v3_measure(
        self, api_client, db_session
    ):
        household = await self.household_with_a_card_and_a_loan(
            api_client, db_session, "+14165579131", link=False
        )

        covered = await covered_minimums(
            db_session, household, "CAD", date(2026, 8, 1), 100_000
        )

        assert covered == 40_000, "min(debt paid, all minimums)"


class TestTheDebtLine:
    async def test_a_linked_budgeted_cards_minimum_is_left_out(
        self, api_client, db_session
    ):
        household, _ = await a_household(api_client, "+14165579140")
        card = (
            await api_client.post(
                "/accounts", json={"name": "Visa card", "kind": "credit_card"}
            )
        ).json()["id"]
        groceries = await a_system_category_id(db_session, "groceries")
        db_session.add_all(
            [
                Debt(
                    household_id=household,
                    name="Visa",
                    balance_minor_units=200_000,
                    minimum_payment_minor_units=10_000,
                    currency="CAD",
                    account_id=uuid.UUID(card),
                ),
                Debt(
                    household_id=household,
                    name="Loan",
                    balance_minor_units=500_000,
                    minimum_payment_minor_units=30_000,
                    currency="CAD",
                ),
                tx(
                    household,
                    uuid.UUID(card),
                    minor=8_000,
                    month=8,
                    day=3,
                    category_id=groceries,
                ),
            ]
        )
        await db_session.commit()
        window = [date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)]

        with_card = await budget_service._minimums(db_session, household, "CAD", window)
        no_window = await budget_service._minimums(db_session, household, "CAD", [])

        assert (
            with_card == 30_000
        ), "the card's purchases are budgeted; only the loan's minimum"
        assert no_window == 40_000, "nothing budgeted yet: every minimum counts"
