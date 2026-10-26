"""Your plan's diagnostics, end to end, with a fake model.

What matters most: the figures are the plan's own arithmetic on the person's
savings amounts; the AI's words carry no figures and its input no names; and
the model is asked only when the figures changed, at most four times a month.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.api import plan as endpoint
from tests.conftest import requires_db
from tests.test_ai_budget_api import FakeModel
from tests.test_budgets_api import a_ready_household
from tests.test_dashboard import a_household, setup_wizard
from tests.test_plan_api import TODAY, a_rate_on_the_visa
from tests.test_statements_endpoint import consent_to_ai

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

ALLOCATION = "/plan/diagnostics/allocation"
INVEST = "/plan/diagnostics/invest"

ALLOCATION_ANSWER = {
    "headline": "Fill your savings first, then pay down the card.",
    "steps": [
        {"to": "savings", "why": "A cushion keeps a surprise off the card."},
        {"to": "debt", "why": "The card costs about 21% a year."},
        {"to": "lottery", "why": "Not a step."},
    ],
    "note": "Your savings amount fits your spending. Revisit it in 6 months.",
}
INVEST_ANSWER = {
    "headline": "Not yet.",
    "why": "Your card charges about 21% a year. Clear it first.",
    "where": "Later, a tax-advantaged savings account is a common place to start.",
    "next": "Come back once the card is paid off.",
}


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    for module in ("plan", "budgets"):
        monkeypatch.setattr(f"app.api.{module}._today", lambda: TODAY)


def use_model(monkeypatch, model) -> None:
    monkeypatch.setattr(endpoint, "build_client", lambda _settings: model)


async def ready(api_client, db_session, phone: str) -> None:
    await a_ready_household(api_client, db_session, phone)
    await a_rate_on_the_visa(api_client)
    await consent_to_ai(api_client)


def money(text: str | None) -> Decimal:
    return Decimal(text or "0")


class TestWhereTheMoneyGoes:
    async def test_the_plan_runs_on_the_persons_savings_figures(
        self, api_client, db_session, monkeypatch
    ):
        await ready(api_client, db_session, "+14165579501")
        model = FakeModel(ALLOCATION_ANSWER)
        use_model(monkeypatch, model)

        response = await api_client.post(
            ALLOCATION, json={"savings_target": "1000", "savings_now": "400"}
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["stored"] is False
        assert body["savings_gap"] == "600.00"
        assert body["to_savings"] == "600.00", "savings first, up to the gap"
        assert money(body["to_debt"]) > 0
        assert body["debt_first"] == "Visa"
        assert body["debt_first_rate_percent"] == "20.99"
        assert money(body["debt_per_100"]) > money(body["invest_per_100"])
        total = sum(
            money(body[k]) for k in ("to_savings", "to_goals", "to_debt", "to_invest")
        )
        assert total == money(body["leftover"]), "every dollar left over is placed"
        # Words only: the sentence with "21%" is gone, so is the made-up step.
        assert [s["to"] for s in body["steps"]] == ["savings"]
        assert body["note"] == "Your savings amount fits your spending."
        sent = model.calls[0][1]
        assert "Visa" not in sent, "no names go to the model"
        assert json.loads(sent)["savings_target"] == "1000.00"

    async def test_the_same_figures_return_the_stored_answer(
        self, api_client, db_session, monkeypatch
    ):
        await ready(api_client, db_session, "+14165579502")
        model = FakeModel(ALLOCATION_ANSWER)
        use_model(monkeypatch, model)
        figures = {"savings_target": "1000", "savings_now": "400"}

        await api_client.post(ALLOCATION, json=figures)
        again = await api_client.post(ALLOCATION, json=figures)

        assert again.json()["stored"] is True
        assert len(model.calls) == 1

    async def test_four_fresh_runs_a_month(self, api_client, db_session, monkeypatch):
        await ready(api_client, db_session, "+14165579503")
        model = FakeModel(ALLOCATION_ANSWER)
        use_model(monkeypatch, model)

        codes = [
            (
                await api_client.post(
                    ALLOCATION,
                    json={"savings_target": str(1000 + i), "savings_now": "0"},
                )
            ).status_code
            for i in range(5)
        ]
        listed = (await api_client.get("/plan/diagnostics")).json()

        assert codes == [200, 200, 200, 200, 429]
        assert len(model.calls) == 4
        assert listed["runs_left"]["allocation"] == 0
        assert listed["allocation"]["savings_target"] == "1003.00", "the last one stays"
        assert listed["savings_target"] == "1003.00", "the form remembers"


class TestShouldIInvest:
    async def test_what_comes_first_is_checked_by_the_server(
        self, api_client, db_session, monkeypatch
    ):
        await ready(api_client, db_session, "+14165579601")
        use_model(monkeypatch, FakeModel(INVEST_ANSWER))

        body = (
            await api_client.post(
                INVEST, json={"savings_target": "1000", "savings_now": "0"}
            )
        ).json()

        assert body["buffer_full"] is False
        assert body["expensive_debts"] == ["Visa"]
        assert body["goals_covered"] is True, "no goals asks nothing"
        assert body["assumed_return_percent"] == "5.00"
        assert body["headline"] == "Not yet."
        assert body["why"] == "Clear it first.", "the sentence with a figure is gone"
        assert body["where"].startswith("Later, a tax-advantaged")


class TestTheGates:
    async def test_not_while_still_learning(self, api_client, monkeypatch):
        await a_household(api_client, "+14165579701")
        await setup_wizard(api_client, income="5000.00")
        await consent_to_ai(api_client)
        model = FakeModel(ALLOCATION_ANSWER)
        use_model(monkeypatch, model)

        response = await api_client.post(ALLOCATION, json={})

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "still_learning"
        assert model.calls == []

    async def test_without_consent_the_model_is_never_asked(
        self, api_client, db_session, monkeypatch
    ):
        await a_ready_household(api_client, db_session, "+14165579702")
        model = FakeModel(ALLOCATION_ANSWER)
        use_model(monkeypatch, model)

        response = await api_client.post(ALLOCATION, json={})

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "consent_required"
        assert model.calls == []

    async def test_before_any_run_the_form_starts_from_the_plan(
        self, api_client, db_session
    ):
        await a_ready_household(api_client, db_session, "+14165579703")

        body = (await api_client.get("/plan/diagnostics")).json()

        assert body["allocation"] is None and body["invest"] is None
        assert body["currency"] == "CAD"
        assert money(body["savings_target"]) > 0, "the plan's suggested buffer"
        assert body["savings_now"] == "0.00"
        assert body["runs_left"] == {"allocation": 4, "invest": 4}
