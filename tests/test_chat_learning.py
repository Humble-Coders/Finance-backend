"""Holding back advice while the coach is still learning (#72).

A household with no complete month and fewer than 20 transactions gets facts,
never advice. The table tests need no database; the rest run the chat end to
end with a scripted model. "Today" is 2026-09-15, so September is still
running and does not count as a complete month.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest
from sqlalchemy import select

from app.models.derived import ChatDailyUsage
from app.services.chat_learning import (
    ADVICE,
    FACT,
    LEARNING_INSTRUCTION,
    UNCLEAR,
    classify,
    prescriptive,
)
from tests.chat_support import (
    a_chat_household,
    a_conversation,
    a_learning_household,
    ask,
    call,
    say,
    say_from,
)
from tests.conftest import requires_db
from tests.test_budgets_api import TODAY
from tests.test_dashboard import tx

LEARNING = {
    "ready": False,
    "complete_months": 0,
    "transactions": 5,
    "needs": {"complete_months": 1, "transactions": 20},
}


# --- No database ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "kind"),
    [
        ("How much did I spend this month?", FACT),
        ("What did I spend at Costco?", FACT),
        ("What's my biggest category?", FACT),
        ("Show me my Amazon purchases", FACT),
        ("How many transactions do I have?", FACT),
        ("Explain my score", FACT),
        ("Why did my spending go up?", FACT),
        # Looking back is a fact, even with a word about what to do (#90):
        ("How much did I invest last year?", FACT),
        ("Did my score improve this month?", FACT),
        ("Did I spend less than last month?", FACT),
        ("Which card did I use most?", FACT),
        ("What did the app suggest for my groceries budget?", FACT),
        ("Did I afford the trip in July?", FACT),
        ("How much should I save each month?", ADVICE),
        ("What should I budget for groceries?", ADVICE),
        ("Can I afford a $400 trip?", ADVICE),
        ("How do I improve my score?", ADVICE),
        ("Should I pay off the Visa first?", ADVICE),
        ("Give me some tips to spend less", ADVICE),
        ("Help me make a plan for my debt", ADVICE),
        ("Should I invest?", ADVICE),
        ("Which debt to pay first?", ADVICE),
        ("Hello there", UNCLEAR),
        ("Groceries", UNCLEAR),
    ],
)
def test_each_question_is_classified(question, kind):
    assert classify(question) == kind


@pytest.mark.parametrize(
    ("reply", "instructs"),
    [
        ("You should put $200 a month toward savings.", True),
        ("I recommend cutting dining to $150.", True),
        ("Consider paying the Visa first.", True),
        ("Try to keep groceries under $400.", True),
        ("Aim for $200 a month.", True),
        # Pointing at a screen is not advice (#90):
        ("Try to look at the Transactions screen for each one.", False),
        ("To see every row, make sure you open the Transactions screen.", False),
        ("You spent $40.00 at Costco in September.", False),
        ("Your biggest category is groceries.", False),
    ],
)
def test_prescriptive_replies_are_spotted(reply, instructs):
    assert prescriptive(reply) is instructs


# --- End to end -------------------------------------------------------------------


@pytest.fixture
def pinned_today(monkeypatch):
    monkeypatch.setattr("app.api.chat._today", lambda: TODAY)
    monkeypatch.setattr(
        "app.api.chat._now", lambda: datetime.combine(TODAY, time(12), UTC)
    )
    monkeypatch.setattr("app.api.budgets._today", lambda: TODAY)
    monkeypatch.setattr("app.api.chat_suggestions._today", lambda: TODAY)


THIS_MONTH = dict(from_month="2026-09", to_month="2026-09", group_by="none")


@pytest.mark.usefixtures("pinned_today")
@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
@requires_db
class TestWhileLearning:
    async def test_a_fact_is_answered_and_the_progress_comes_with_it(
        self, api_client, db_session, model
    ):
        await a_learning_household(api_client, db_session, "+14165578901")
        model(
            call("spending_summary", **THIS_MONTH),
            say_from(lambda r: f"You've spent ${float(r['total']):,.2f} this month."),
        )
        conversation = await a_conversation(api_client)

        response = await ask(
            api_client, conversation, "How much did I spend this month?"
        )

        body = response.json()
        assert body["answered_by"] == "model"
        assert body["text"] == "You've spent $50.10 this month."
        assert body["learning"] == LEARNING

    async def test_advice_gets_the_learning_reply_and_no_model(
        self, api_client, db_session, no_model
    ):
        household, _ = await a_learning_household(
            api_client, db_session, "+14165578902", consent=False
        )
        conversation = await a_conversation(api_client)

        response = await ask(
            api_client, conversation, "How much should I save each month?"
        )

        body = response.json()
        assert response.status_code == 200, "no consent needed: no model"
        assert body["answered_by"] == "template"
        assert "0 of 1 full month · 5 of 20 transactions" in body["text"]
        assert body["learning"] == LEARNING
        used = await db_session.scalar(
            select(ChatDailyUsage.model_answers).where(
                ChatDailyUsage.household_id == household
            )
        )
        assert used is None, "not counted against the cap"

    async def test_an_unclear_question_reaches_the_model_told_facts_only(
        self, api_client, db_session, model
    ):
        await a_learning_household(api_client, db_session, "+14165578903")
        scripted = model(say("Hi — ask me about what you've spent."))
        conversation = await a_conversation(api_client)

        await ask(api_client, conversation, "Hello there")

        system = scripted.calls[0]["system"]
        assert system.endswith(LEARNING_INSTRUCTION), "after the cached prefix"

    async def test_a_model_that_advises_anyway_is_replaced(
        self, api_client, db_session, model
    ):
        await a_learning_household(api_client, db_session, "+14165578904")
        # No figures, so grounding passes it: only the advice check is left.
        model(say("You should cut back on groceries from now on."))
        conversation = await a_conversation(api_client)

        response = await ask(api_client, conversation, "Groceries")

        body = response.json()
        assert "still learning" in body["text"]
        assert "cut back" not in body["text"]

    async def test_a_suggestion_is_answered(self, api_client, db_session, no_model):
        await a_learning_household(api_client, db_session, "+14165578905")
        conversation = await a_conversation(api_client)

        response = await ask(api_client, conversation, suggestion_id="spent_this_year")

        body = response.json()
        assert body["text"] == "You've spent $50.10 so far in 2026."
        assert body["learning"] == LEARNING


@pytest.mark.usefixtures("pinned_today")
@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
@requires_db
class TestOnceReady:
    async def test_advice_goes_to_the_model_and_learning_is_null(
        self, api_client, db_session, model
    ):
        await a_chat_household(api_client, db_session, "+14165578911")
        scripted = model(
            call("budget", month="2026-09"),
            say("Your budget has room for a little more saving this month."),
        )
        conversation = await a_conversation(api_client)

        response = await ask(
            api_client, conversation, "How much should I save each month?"
        )

        body = response.json()
        assert body["learning"] is None
        assert body["answered_by"] == "model"
        assert not scripted.calls[0]["system"].endswith(LEARNING_INSTRUCTION)

    async def test_the_line_is_learning_states_own(self, api_client, db_session, model):
        """Crossing the threshold is a change to the data, not to a copy of
        the rule: a full August and 20 transactions, and advice is answered."""
        household, account = await a_learning_household(
            api_client, db_session, "+14165578912"
        )
        conversation = await a_conversation(api_client)
        before = await ask(api_client, conversation, "How much should I save?")
        assert "still learning" in before.json()["text"]

        db_session.add_all(
            tx(
                household,
                account,
                minor=2_000 + i,
                month=8,
                day=1 + i % 28,
                description=f"AUGUST {i}",
            )
            for i in range(20)
        )
        await db_session.commit()
        model(say("Here is what your numbers allow."))

        after = await ask(api_client, conversation, "How much should I save?")

        assert after.json()["answered_by"] == "model"
        assert after.json()["learning"] is None
