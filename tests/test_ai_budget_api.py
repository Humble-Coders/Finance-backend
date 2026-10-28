"""`/budgets/ai` — the AI's budget suggestions, end to end, with a fake model.

What matters most: whatever the model answers, the figures a person sees
add up and hold their commitments; nothing but figures is sent (no names);
and the model is asked at most as often as the limits say.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.api import ai_budget as endpoint
from tests.conftest import requires_db
from tests.test_budgets_api import SEPTEMBER, TODAY, a_ready_household, by_slug
from tests.test_dashboard import a_household, setup_wizard
from tests.test_statements_endpoint import consent_to_ai

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

SETUP = "/budgets/ai/setup"
REVIEW = f"{SEPTEMBER}/ai/review"


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr("app.api.budgets._today", lambda: TODAY)


class FakeModel:
    model = "fake-model"

    def __init__(self, answer: dict | str):
        self.answer = answer if isinstance(answer, str) else json.dumps(answer)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, *, system: str, user: str, max_output_tokens: int) -> str:
        self.calls.append((system, user))
        return self.answer


def use_model(monkeypatch, model) -> None:
    monkeypatch.setattr(endpoint, "build_client", lambda _settings: model)


ANSWER = {
    "lines": [
        {"category": "rent", "amount": "1500", "reason": "Your rent."},
        {"category": "groceries", "amount": "600", "reason": "Food at home."},
        {"category": "dining", "amount": "900", "reason": "Eating out, kept modest."},
        {"category": "shopping", "amount": "2000", "reason": "You spend $2000 here."},
        {"category": "income", "amount": "100", "reason": "Not a line."},
    ],
    "note": "Needs come first. About 20% goes to savings.",
}


async def a_set_up_household(api_client, phone: str) -> None:
    await a_household(api_client, phone)
    await setup_wizard(
        api_client,
        income="5000.00",
        obligations=[
            {"name": "Rent", "monthly_amount": "1800.00", "category": "rent"},
            {"name": "Netflix", "monthly_amount": "16.49", "category": "subscriptions"},
        ],
        debts=[{"name": "Visa", "balance": "2000.00", "minimum_payment": "120.00"}],
    )
    await consent_to_ai(api_client)


def money(text: str) -> Decimal:
    return Decimal(text)


class TestTheFirstBudget:
    async def test_the_suggestion_adds_up_and_holds_the_commitments(
        self, api_client, monkeypatch
    ):
        await a_set_up_household(api_client, "+14165578001")
        model = FakeModel(ANSWER)
        use_model(monkeypatch, model)

        response = await api_client.post(SETUP)

        assert response.status_code == 200, response.text
        body = response.json()
        lines = {line["slug"]: line for line in body["lines"]}
        assert money(lines["rent"]["amount"]) == Decimal(
            "1800.00"
        ), "held at the commitment"
        assert lines["rent"]["committed"] == "1800.00"
        assert lines["subscriptions"]["amount"] == "17.00", "the commitment, rounded up"
        assert lines["debt_payment"]["kind"] == "debt"
        assert lines["debt_payment"]["amount"] == "120.00"
        assert "income" not in lines
        total = sum(money(line["amount"]) for line in body["lines"]) + money(
            body["savings"]
        )
        assert total == Decimal("5000.00"), "lines, debt and savings are the income"
        assert money(body["savings"]) >= Decimal("1000.00"), "at least a fifth"
        assert money(body["wants"]) <= Decimal("1500.00"), "wants within their share"
        assert body["needs_percent"] + body["wants_percent"] + body[
            "savings_percent"
        ] in (
            99,
            100,
            101,
        )
        assert lines["shopping"]["reason"] is None, "a reason with a figure is dropped"
        assert lines["dining"]["reason"] == "Eating out, kept modest."
        assert body["note"] == "Needs come first.", "the sentence with a figure is gone"
        assert body["attempts_left"] == 2

    async def test_only_figures_are_sent(self, api_client, monkeypatch):
        await a_set_up_household(api_client, "+14165578002")
        await api_client.post(
            "/goals",
            json={
                "name": "Trip to Lisbon",
                "kind": "vacation",
                "horizon": "short_term",
                "target": "3000.00",
                "target_date": "2027-06-30",
            },
        )
        model = FakeModel(ANSWER)
        use_model(monkeypatch, model)

        await api_client.post(SETUP)

        _, sent = model.calls[0]
        for name in ("Netflix", "Visa", "Lisbon", "Rent"):
            assert name not in sent
        payload = json.loads(sent)
        assert payload["monthly_income"] == "5000.00"
        assert payload["commitments_by_category"] == {
            "rent": "1800.00",
            "subscriptions": "17.00",
        }

    async def test_three_tries_then_the_last_one_stays(self, api_client, monkeypatch):
        await a_set_up_household(api_client, "+14165578003")
        model = FakeModel(ANSWER)
        use_model(monkeypatch, model)

        for _ in range(3):
            assert (await api_client.post(SETUP)).status_code == 200
        fourth = await api_client.post(SETUP)
        kept = await api_client.get(SETUP)

        assert fourth.status_code == 429
        assert fourth.json()["detail"]["code"] == "ai_budget_limit_reached"
        assert len(model.calls) == 3
        assert kept.status_code == 200
        assert kept.json()["attempts_left"] == 0

    async def test_without_consent_the_model_is_never_asked(
        self, api_client, monkeypatch
    ):
        await a_household(api_client, "+14165578004")
        await setup_wizard(api_client, income="5000.00")
        model = FakeModel(ANSWER)
        use_model(monkeypatch, model)

        response = await api_client.post(SETUP)

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "consent_required"
        assert model.calls == []

    async def test_an_answer_that_is_not_json_stores_nothing(
        self, api_client, monkeypatch
    ):
        await a_set_up_household(api_client, "+14165578005")
        use_model(monkeypatch, FakeModel("Sorry, I can't help with that."))

        response = await api_client.post(SETUP)

        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "ai_unavailable"
        assert (await api_client.get(SETUP)).status_code == 404

    async def test_starting_writes_the_lines_as_the_persons_own(
        self, api_client, monkeypatch
    ):
        await a_set_up_household(api_client, "+14165578006")
        use_model(monkeypatch, FakeModel(ANSWER))
        proposal = (await api_client.post(SETUP)).json()
        lines = {line["slug"]: line for line in proposal["lines"]}

        response = await api_client.post(
            f"{SETUP}/accept",
            json={
                "lines": [
                    {"category_id": lines["rent"]["category_id"], "amount": "1800.00"},
                    {"category_id": lines["groceries"]["category_id"], "amount": "550"},
                    {
                        "category_id": lines["debt_payment"]["category_id"],
                        "amount": "999",
                    },
                ],
                "savings": "900",
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        mine = by_slug(body)
        assert mine["groceries"]["allocated"] == "550.00"
        assert mine["groceries"]["is_user_set"] is True
        assert body["savings"]["allocated"] == "900.00"
        assert body["savings"]["is_user_set"] is True
        # The Debt line follows the minimums, whatever was sent for it.
        assert (body["debt"] or {}).get("allocated") != "999.00"
        # The AI's suggestion stays visible beside each line.
        assert mine["groceries"]["ai_suggested"] is not None


class TestTheMonthlyReview:
    async def test_it_suggests_each_line_once_a_month_with_advice(
        self, api_client, db_session, monkeypatch
    ):
        await a_ready_household(api_client, db_session, "+14165578101")
        await consent_to_ai(api_client)
        model = FakeModel(
            {
                "lines": [
                    {
                        "category": "groceries",
                        "amount": "430",
                        "reason": "Close to usual.",
                    },
                    {"category": "dining", "amount": "60", "reason": "A little room."},
                ],
                "advice": "Dining went over. Put what is left toward your buffer. "
                "Last month you saved $500.",
            }
        )
        use_model(monkeypatch, model)
        before = (await api_client.get(SEPTEMBER)).json()
        assert before["ai_review_due"] is True

        first = await api_client.post(REVIEW)
        second = await api_client.post(REVIEW)

        assert first.status_code == 200, first.text
        assert len(model.calls) == 1, "once a month"
        body = second.json()
        assert body["ai_kind"] == "review"
        assert body["ai_review_due"] is False
        assert by_slug(body)["groceries"]["ai_suggested"] == "430.00"
        assert (
            by_slug(body)["groceries"]["allocated"] == "450.00"
        ), "nothing changed by itself"
        assert (
            body["ai_advice"]
            == "Dining went over. Put what is left toward your buffer."
        )
        assert [
            u["ai_suggested"] for u in body["unbudgeted"] if u["slug"] == "dining"
        ] == ["60.00"]
        payload = json.loads(model.calls[0][1])
        assert any(row["category"] == "groceries" for row in payload["last_month"])

    async def test_only_the_month_you_are_in(self, api_client, db_session, monkeypatch):
        await a_ready_household(api_client, db_session, "+14165578102")
        await consent_to_ai(api_client)
        use_model(monkeypatch, FakeModel(ANSWER))

        response = await api_client.post("/budgets/2026-08/ai/review")

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "not_current_month"

    async def test_not_while_still_learning(self, api_client, monkeypatch):
        await a_set_up_household(api_client, "+14165578103")
        model = FakeModel(ANSWER)
        use_model(monkeypatch, model)

        response = await api_client.post(REVIEW)

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "still_learning"
        assert model.calls == []


class TestUsualSpending:
    async def test_the_last_three_months_are_the_reference(
        self, api_client, db_session, monkeypatch
    ):
        # Groceries $400 / $500 / $450 in June–August: usually $450.
        # Dining once ($90): a one-off, no usual amount.
        await a_ready_household(api_client, db_session, "+14165578201")
        await consent_to_ai(api_client)
        model = FakeModel(
            {
                "lines": [
                    {"category": "dining", "amount": "60", "reason": "A little room."}
                ],
                "note": "Built from your usual spending.",
            }
        )
        use_model(monkeypatch, model)

        body = (await api_client.post(SETUP)).json()

        payload = json.loads(model.calls[0][1])
        assert payload["usual_monthly_spending"] == {"groceries": "450.00"}
        lines = {line["slug"]: line for line in body["lines"]}
        # The model left groceries out; it usually is spent, so it stays.
        assert lines["groceries"]["amount"] == "450.00"
        assert lines["groceries"]["usual"] == "450.00"
        assert lines["dining"]["usual"] is None, "a one-off is no usual amount"

    async def test_a_new_account_has_no_usual_spending(self, api_client, monkeypatch):
        await a_set_up_household(api_client, "+14165578202")
        model = FakeModel(ANSWER)
        use_model(monkeypatch, model)

        body = (await api_client.post(SETUP)).json()

        assert json.loads(model.calls[0][1])["usual_monthly_spending"] == {}
        assert all(line["usual"] is None for line in body["lines"])
