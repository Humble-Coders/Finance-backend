"""Suggested questions answered without AI (#84).

Every figure in an answer is checked against the tool (or the Budget tab)
that the answer says it comes from. "Today" is pinned to 2026-09-15, as in
`test_budgets_api`, whose ready household these tests use.
"""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.api import chat_suggestions
from app.models.identity import Household
from app.services import chat_templates, chat_tools, rollups
from tests.conftest import requires_db
from tests.test_budgets_api import SEPTEMBER, TODAY, a_ready_household
from tests.test_dashboard import a_household, tx

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

LIST = "/chat/suggestions"


def answer_url(suggestion_id: str) -> str:
    return f"{LIST}/{suggestion_id}/answer"


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr(chat_suggestions, "_today", lambda: TODAY)
    monkeypatch.setattr("app.api.budgets._today", lambda: TODAY)


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    """Fails the test if anything on these paths builds a model client."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("a suggested answer built a model client")

    for module in (
        "app.services.llm",
        "app.services.filing",
        "app.api.statements",
        "app.api.plan",
        "app.api.ai_budget",
    ):
        monkeypatch.setattr(f"{module}.build_client", refuse)


async def chat_on(db_session) -> None:
    await db_session.execute(
        text(
            "UPDATE feature_availability SET is_enabled = true "
            "WHERE feature_key = 'ai_chat'"
        )
    )


async def a_household_with_months(api_client, db_session, phone: str):
    household, account, ids = await a_ready_household(api_client, db_session, phone)
    # Inserted directly by the helper, so the totals are built as the
    # backfill builds them.
    await rollups.rebuild(db_session, household)
    await chat_on(db_session)
    await db_session.commit()
    return household, account, ids


async def tool(db_session, household, function, **args):
    return await function(db_session, household, "CAD", TODAY, **args)


async def answered(api_client, suggestion_id: str) -> dict:
    response = await api_client.post(answer_url(suggestion_id))
    assert response.status_code == 200, response.text
    return response.json()


class TestTheList:
    async def test_all_six_are_offered_when_there_is_data(self, api_client, db_session):
        await a_household_with_months(api_client, db_session, "+14165578501")

        response = await api_client.get(LIST)

        assert response.status_code == 200, response.text
        assert [s["id"] for s in response.json()["suggestions"]] == [
            s.id for s in chat_templates.SUGGESTIONS
        ]
        assert response.json()["suggestions"][0]["text"] == (
            "How much did I spend last month?"
        )

    async def test_a_household_with_nothing_is_offered_nothing(
        self, api_client, db_session
    ):
        await a_household(api_client, "+14165578502")
        await chat_on(db_session)

        response = await api_client.get(LIST)

        assert response.json()["suggestions"] == []

    async def test_one_with_no_data_is_left_out(self, api_client, db_session):
        """August has spending; September none yet, so the questions about
        this month have nothing to say."""
        household, account = await a_household(api_client, "+14165578503")
        db_session.add(tx(household, account, minor=4_200, month=8, day=3))
        await db_session.flush()
        await rollups.rebuild(db_session, household)
        await chat_on(db_session)

        offered = [s["id"] for s in (await api_client.get(LIST)).json()["suggestions"]]

        assert offered == ["spent_last_month", "spent_this_year", "top_merchants_year"]


class TestEachAnswerIsTheToolsFigure:
    async def test_spent_last_month(self, api_client, db_session):
        household, _, _ = await a_household_with_months(
            api_client, db_session, "+14165578511"
        )

        body = await answered(api_client, "spent_last_month")

        august = await tool(
            db_session,
            household,
            chat_tools.spending_summary,
            from_month="2026-08",
            to_month="2026-08",
            group_by="none",
        )
        assert august["total"] == "840.00", "groceries, dining and debt; not transfers"
        assert body["figures"] == ["840.00"]
        assert body["text"] == "You spent $840.00 in August."

    async def test_biggest_category(self, api_client, db_session):
        household, _, _ = await a_household_with_months(
            api_client, db_session, "+14165578512"
        )

        body = await answered(api_client, "biggest_category")

        september = await tool(
            db_session,
            household,
            chat_tools.spending_summary,
            from_month="2026-09",
            to_month="2026-09",
            group_by="category",
        )
        top = september["rows"][0]
        assert body["figures"] == [top["t"], september["total"]]
        assert "$300.00" in body["text"] and "$507.35" in body["text"]

    async def test_vs_last_month(self, api_client, db_session):
        household, _, _ = await a_household_with_months(
            api_client, db_session, "+14165578513"
        )

        body = await answered(api_client, "vs_last_month")

        compared = await tool(
            db_session,
            household,
            chat_tools.period_compare,
            a_from="2026-08",
            a_to="2026-08",
            b_from="2026-09",
            b_to="2026-09",
        )
        total = compared["total"]
        assert body["figures"] == [total["b"], total["a"], total["diff"].lstrip("-")]
        assert body["text"].endswith("$332.65 less.")

    async def test_spent_this_year(self, api_client, db_session):
        household, _, _ = await a_household_with_months(
            api_client, db_session, "+14165578514"
        )

        body = await answered(api_client, "spent_this_year")

        year = await tool(
            db_session,
            household,
            chat_tools.spending_summary,
            from_month="2026-01",
            to_month="2026-09",
            group_by="none",
        )
        assert body["figures"][0] == year["total"]
        assert body["text"].startswith(f"You've spent ${Decimal(year['total']):,.2f}")

    async def test_top_merchants_this_year(self, api_client, db_session):
        household, _, _ = await a_household_with_months(
            api_client, db_session, "+14165578515"
        )

        body = await answered(api_client, "top_merchants_year")

        top = await tool(
            db_session,
            household,
            chat_tools.top_merchants,
            from_date="2026-01-01",
            to_date="2026-09-15",
            limit=3,
        )
        assert body["figures"] == [r["t"] for r in top["rows"]]
        for row in top["rows"]:
            assert row["m"] in body["text"]

    async def test_budget_left_is_the_budget_tabs_figure(self, api_client, db_session):
        await a_household_with_months(api_client, db_session, "+14165578516")

        body = await answered(api_client, "budget_left")

        budget = (await api_client.get(SEPTEMBER)).json()
        left = Decimal(budget["left_to_spend"])
        figure = Decimal(body["figures"][0])
        assert figure == abs(left)
        word = "left to spend" if left >= 0 else "over your budget"
        assert word in body["text"]


class TestLeftToSpend:
    async def test_it_is_the_apps_rule_on_the_server(self, api_client, db_session):
        """Every line but savings, less everything spent so far — what the
        app's BudgetBoard.leftToSpend worked out on the phone."""
        await a_household_with_months(api_client, db_session, "+14165578521")

        budget = (await api_client.get(SEPTEMBER)).json()

        savings = Decimal(budget["savings"]["allocated"]) if budget["savings"] else 0
        assert Decimal(budget["left_to_spend"]) == (
            Decimal(budget["total_allocated"])
            - savings
            - Decimal(budget["spent_overall"])
        )

    async def test_it_is_null_once_the_month_has_ended(self, api_client, db_session):
        await a_household_with_months(api_client, db_session, "+14165578522")

        budget = (await api_client.get("/budgets/2026-08")).json()

        assert budget["month_ended"] is True
        assert budget["left_to_spend"] is None


class TestNoModelAndNoAiGates:
    async def test_every_path_works_without_ai_consent_or_a_model(
        self, api_client, db_session
    ):
        """`a_ready_household` never consents to AI processing, and the
        `no_model` fixture fails on any model client."""
        await a_household_with_months(api_client, db_session, "+14165578531")

        offered = (await api_client.get(LIST)).json()["suggestions"]
        for suggestion in offered:
            await answered(api_client, suggestion["id"])

        assert len(offered) == 6

    async def test_nothing_here_reads_consent_or_the_tier(self):
        for module in (chat_suggestions, chat_templates):
            source = inspect.getsource(module)
            for gate in ("has_consented", "llm_no_training_tier", "build_client"):
                assert gate not in source, f"{module.__name__} reads {gate}"


class TestTheCache:
    async def test_a_ledger_write_changes_the_next_answer(self, api_client, db_session):
        household, _, _ = await a_household_with_months(
            api_client, db_session, "+14165578541"
        )
        before = await answered(api_client, "spent_last_month")
        row = (
            await db_session.execute(
                text(
                    "SELECT id FROM transaction WHERE household_id = :h "
                    "AND description = 'BISTRO' AND occurred_on = '2026-08-12'"
                ),
                {"h": household},
            )
        ).scalar_one()

        response = await api_client.patch(
            f"/transactions/{row}", json={"amount": "100.00"}
        )

        assert response.status_code == 200, response.text
        after = await answered(api_client, "spent_last_month")
        assert before["figures"][0] == "840.00"
        assert after["figures"][0] == "850.00"

    async def test_the_same_version_is_served_from_the_cache(
        self, api_client, db_session, monkeypatch
    ):
        await a_household_with_months(api_client, db_session, "+14165578542")
        await answered(api_client, "spent_this_year")

        async def broken(*_args, **_kwargs):
            raise AssertionError("asked the tools again")

        monkeypatch.setattr(chat_tools, "spending_summary", broken)

        await answered(api_client, "spent_this_year")

    async def test_a_cached_answer_cannot_be_changed_by_its_caller(
        self, api_client, db_session
    ):
        household, _, _ = await a_household_with_months(
            api_client, db_session, "+14165578543"
        )
        home = await db_session.get(Household, household)
        first = await chat_templates.answer(db_session, home, "spent_last_month", TODAY)

        assert isinstance(first.figures, tuple)
        with pytest.raises(AttributeError):
            first.figures.append("1.00")
        again = await chat_templates.answer(db_session, home, "spent_last_month", TODAY)
        assert again.figures == ("840.00",)


class TestBoundaries:
    async def test_chat_off_is_403(self, api_client, db_session):
        await a_ready_household(api_client, db_session, "+14165578551")

        for response in (
            await api_client.get(LIST),
            await api_client.post(answer_url("spent_last_month")),
        ):
            assert response.status_code == 403
            assert response.json()["detail"]["feature"] == "ai_chat"

    async def test_another_households_data_never_appears(self, api_client, db_session):
        await a_household_with_months(api_client, db_session, "+14165578552")
        await a_household(api_client, "+14165578553")

        assert (await api_client.get(LIST)).json()["suggestions"] == []
        response = await api_client.post(answer_url("spent_last_month"))
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "nothing_to_say"

    async def test_an_unknown_suggestion_is_404(self, api_client, db_session):
        await a_household(api_client, "+14165578554")
        await chat_on(db_session)

        response = await api_client.post(answer_url("what_is_the_meaning_of_life"))

        assert response.status_code == 404

    async def test_no_budget_tab_no_budget_answer(self, api_client, db_session):
        await a_household_with_months(api_client, db_session, "+14165578555")
        await db_session.execute(
            text(
                "UPDATE feature_availability SET is_enabled = false "
                "WHERE feature_key = 'auto_budget'"
            )
        )

        offered = [s["id"] for s in (await api_client.get(LIST)).json()["suggestions"]]

        assert "budget_left" not in offered
        assert len(offered) == 5

    async def test_the_budget_feature_is_the_budget_tabs(self):
        from app.api.budgets import FEATURE

        assert chat_templates.BUDGET_FEATURE == FEATURE


async def test_months_roll_over_the_year():
    ctx = chat_templates._Context(None, None, "CAD", date(2026, 1, 9))

    assert ctx.last_month == date(2025, 12, 1)
    assert ctx.this_month == date(2026, 1, 1)


async def test_amounts_read_like_money():
    from app.core.money import display

    assert display("1234.5", "CAD") == "$1,234.50"
    assert display("-12", "CAD") == "-$12.00"
    assert display("1200", "JPY") == "1,200 JPY"
