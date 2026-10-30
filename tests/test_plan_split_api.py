"""Your plan's "Where it goes" (redesign, 2026-10-31), end to end.

What matters most: the AI proposes the amounts and the server holds them to
its limits; the AI is asked only when the figures change; a row set by hand
rebalances the others, survives the next read, and — when the person says so —
is put where it belongs.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.services.llm import LlmError
from tests.conftest import requires_db
from tests.test_ai_budget_api import FakeModel
from tests.test_budgets_api import a_ready_household
from tests.test_plan_api import a_rate_on_the_visa
from tests.test_plan_diagnostics import pinned, use_model  # noqa: F401
from tests.test_statements_endpoint import consent_to_ai

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

SPLIT = "/plan/split"


def money(text: str | None) -> Decimal:
    return Decimal(text or "0")


def total(split: dict) -> Decimal:
    parts = money(split["extra_debt"]) + money(split["save_or_invest"])
    parts += sum(money(g["amount"]) for g in split["goals"])
    if split["emergency_fund"]:
        parts += money(split["emergency_fund"]["amount"])
    return parts


async def ready(api_client, db_session, phone: str) -> dict:
    """A ready household with a priced Visa, consent given, and a goal."""
    await a_ready_household(api_client, db_session, phone)
    await a_rate_on_the_visa(api_client)
    await consent_to_ai(api_client)
    goal = await api_client.post(
        "/goals",
        json={
            "name": "Trip to Lisbon",
            "kind": "vacation",
            "horizon": "short_term",
            "target": "3000.00",
        },
    )
    assert goal.status_code == 201, goal.text
    return goal.json()


def answer(debt: str, save: str, goal: str = "0", why: str = "") -> dict:
    return {
        "emergency_fund": "0",
        "debt": debt,
        "goals": [{"index": 0, "amount": goal}],
        "save_or_invest": save,
        "why": why or "The card costs the most, so it comes first.",
    }


class TestTheAiProposes:
    async def test_its_amounts_are_shown_held_to_the_limits(
        self, api_client, db_session, monkeypatch
    ):
        await ready(api_client, db_session, "+14165579901")
        # Far more than the Visa owes, and no goal money: the server cuts the
        # debt to what is owed and places the rest by priority.
        model = FakeModel(answer(debt="999999", save="0", why="Pay 21% first."))
        use_model(monkeypatch, model)

        plan = (await api_client.get("/plan")).json()

        split = plan["split"]
        assert split["source"] == "ai"
        assert total(split) == money(plan["leftover"]), "every dollar placed"
        visa = plan["debts"][0]
        assert money(split["extra_debt"]) <= money(visa["balance"])
        assert split["why"] is None, "a sentence with a figure is dropped"
        sent = model.calls[0][1]
        assert "Lisbon" not in sent and "Visa" not in sent, "no names go out"
        assert json.loads(sent)["money_left_over"] == plan["leftover"]

    async def test_the_same_figures_do_not_ask_again(
        self, api_client, db_session, monkeypatch
    ):
        await ready(api_client, db_session, "+14165579902")
        model = FakeModel(answer(debt="100", save="0"))
        use_model(monkeypatch, model)

        first = (await api_client.get("/plan")).json()["split"]
        again = (await api_client.get("/plan")).json()["split"]

        assert len(model.calls) == 1
        assert again == first

    async def test_new_choices_ask_again(self, api_client, db_session, monkeypatch):
        await ready(api_client, db_session, "+14165579903")
        model = FakeModel(answer(debt="100", save="0"))
        use_model(monkeypatch, model)
        await api_client.get("/plan")

        saved = await api_client.put(
            "/plan/preferences",
            json={
                "strategy": "avalanche",
                "weights": [
                    {"category": "goals", "percent": "70"},
                    {"category": "debt", "percent": "30"},
                ],
            },
        )
        assert saved.status_code == 200, saved.text
        await api_client.get("/plan")

        assert len(model.calls) == 2

    async def test_when_the_ai_fails_the_rules_split_it(
        self, api_client, db_session, monkeypatch
    ):
        await ready(api_client, db_session, "+14165579904")

        class Broken(FakeModel):
            async def complete(self, **_kwargs) -> str:
                raise LlmError("down")

        use_model(monkeypatch, Broken({}))

        plan = (await api_client.get("/plan")).json()

        assert plan["split"]["source"] == "rules"
        assert total(plan["split"]) == money(plan["leftover"])


class TestARowSetByHand:
    async def test_the_others_rebalance_and_the_edit_is_kept(
        self, api_client, db_session, monkeypatch
    ):
        await ready(api_client, db_session, "+14165579911")
        model = FakeModel(answer(debt="100", save="0"))
        use_model(monkeypatch, model)
        before = (await api_client.get("/plan")).json()

        response = await api_client.put(SPLIT, json={"row": "debt", "amount": "50.00"})

        assert response.status_code == 200, response.text
        after = response.json()
        assert after["split"]["extra_debt"] == "50.00"
        assert after["split"]["edited"] is True
        assert total(after["split"]) == money(before["leftover"])
        read = (await api_client.get("/plan")).json()
        assert read["split"] == after["split"], "the edit survives the next read"
        assert len(model.calls) == 1, "and the AI was not asked again"

    async def test_more_than_a_row_can_take_is_refused(self, api_client, db_session):
        await ready(api_client, db_session, "+14165579912")

        response = await api_client.put(
            SPLIT, json={"row": "debt", "amount": "99999999.00"}
        )

        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "over_limit"

    async def test_applied_to_a_goal_it_is_the_goals_monthly_amount(
        self, api_client, db_session
    ):
        goal = await ready(api_client, db_session, "+14165579913")

        response = await api_client.put(
            SPLIT,
            json={
                "row": "goal",
                "goal_id": goal["id"],
                "amount": "25.00",
                "apply": True,
            },
        )

        assert response.status_code == 200, response.text
        goals = (await api_client.get("/goals")).json()["goals"]
        assert goals[0]["monthly_contribution"] == "25.00"
        read = (await api_client.get("/plan")).json()
        trip = next(g for g in read["split"]["goals"] if g["goal_id"] == goal["id"])
        assert trip["amount"] == "25.00", "applying it does not undo the edit"

    async def test_applied_to_debt_it_goes_on_the_budgets_debt_line(
        self, api_client, db_session
    ):
        await ready(api_client, db_session, "+14165579914")
        before = (await api_client.get("/budgets/2026-09")).json()
        plan_before = (await api_client.get("/plan")).json()

        response = await api_client.put(
            SPLIT, json={"row": "debt", "amount": "40.00", "apply": True}
        )

        assert response.status_code == 200, response.text
        budget = (await api_client.get("/budgets/2026-09")).json()
        assert money(budget["debt"]["allocated"]) == money(
            before["debt"]["suggested"]
        ) + Decimal("40.00"), "on top of the minimums"
        plan = (await api_client.get("/plan")).json()
        assert (
            plan["leftover"] == plan_before["leftover"]
        ), "the extra put on the Debt line is still the plan's to split"
        assert plan["split"]["extra_debt"] == "40.00"

    async def test_applied_to_save_or_invest_it_is_the_savings_target(
        self, api_client, db_session
    ):
        await ready(api_client, db_session, "+14165579915")

        response = await api_client.put(
            SPLIT, json={"row": "save_or_invest", "amount": "30.00", "apply": True}
        )

        assert response.status_code == 200, response.text
        budget = (await api_client.get("/budgets/2026-09")).json()
        assert budget["savings"]["allocated"] == "30.00"

    async def test_not_applied_nothing_else_changes(self, api_client, db_session):
        goal = await ready(api_client, db_session, "+14165579916")

        await api_client.put(
            SPLIT, json={"row": "goal", "goal_id": goal["id"], "amount": "25.00"}
        )

        goals = (await api_client.get("/goals")).json()["goals"]
        assert goals[0]["monthly_contribution"] in (None, "0.00")
