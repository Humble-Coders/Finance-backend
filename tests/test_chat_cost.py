"""What the AI coach's chat costs (#71): when it calls no model, and how
little it sends when it does."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from app.models.derived import ChatConversation, ChatDailyUsage
from tests.chat_support import (
    AUGUST,
    a_chat_household,
    a_conversation,
    ask,
    call,
    say,
    say_from,
)
from tests.conftest import requires_db
from tests.test_budgets_api import TODAY

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr("app.api.chat._today", lambda: TODAY)
    monkeypatch.setattr("app.api.budgets._today", lambda: TODAY)
    monkeypatch.setattr("app.api.chat_suggestions._today", lambda: TODAY)


AUGUST_ANSWER = (
    call("spending_summary", **AUGUST),
    say_from(lambda r: f"You spent ${float(r['total']):,.2f} in August."),
)


async def model_answers(db_session, household) -> int:
    used = await db_session.scalar(
        select(ChatDailyUsage.model_answers).where(
            ChatDailyUsage.household_id == household, ChatDailyUsage.day == TODAY
        )
    )
    return used or 0


class TestNoModelCall:
    async def test_a_suggestion_is_answered_without_one(
        self, api_client, db_session, no_model
    ):
        household = await a_chat_household(api_client, db_session, "+14165578801")
        conversation = await a_conversation(api_client)

        response = await ask(api_client, conversation, suggestion_id="spent_last_month")

        assert response.json()["answered_by"] == "template"
        assert await model_answers(db_session, household) == 0

    async def test_the_same_question_again_is_a_cache_hit(
        self, api_client, db_session, model
    ):
        household = await a_chat_household(api_client, db_session, "+14165578802")
        scripted = model(*AUGUST_ANSWER)
        first = await a_conversation(api_client)
        second = await a_conversation(api_client)

        answered = await ask(api_client, first, "What did I spend in August?")
        again = await ask(api_client, second, "  what did i spend in august ")

        assert again.json()["answered_by"] == "cache"
        assert again.json()["text"] == answered.json()["text"]
        assert len(scripted.calls) == 2, "the model ran once, for the first"
        assert await model_answers(db_session, household) == 1, "the hit is free"

    async def test_the_same_question_tomorrow_misses_the_cache(
        self, api_client, db_session, model, monkeypatch
    ):
        """'This month' on the 30th and on the 1st are different questions
        over the same ledger."""
        await a_chat_household(api_client, db_session, "+14165578805")
        scripted = model(*AUGUST_ANSWER, *AUGUST_ANSWER)
        conversation = await a_conversation(api_client)
        await ask(api_client, conversation, "What did I spend in August?")
        monkeypatch.setattr(
            "app.api.chat._today", lambda: TODAY.replace(day=TODAY.day + 1)
        )

        later = await a_conversation(api_client)
        response = await ask(api_client, later, "What did I spend in August?")

        assert response.json()["answered_by"] == "model"
        assert len(scripted.calls) == 4

    async def test_a_ledger_write_misses_the_cache(self, api_client, db_session, model):
        household = await a_chat_household(api_client, db_session, "+14165578803")
        scripted = model(*AUGUST_ANSWER, *AUGUST_ANSWER)
        conversation = await a_conversation(api_client)
        await ask(api_client, conversation, "What did I spend in August?")
        bistro = (
            await db_session.execute(
                text(
                    "SELECT id FROM transaction WHERE household_id = :h "
                    "AND description = 'BISTRO' AND occurred_on = '2026-08-12'"
                ),
                {"h": household},
            )
        ).scalar_one()
        assert (
            await api_client.patch(f"/transactions/{bistro}", json={"amount": "100.00"})
        ).status_code == 200

        fresh = await a_conversation(api_client)
        response = await ask(api_client, fresh, "What did I spend in August?")

        assert response.json()["answered_by"] == "model"
        assert response.json()["text"] == "You spent $850.00 in August."
        assert len(scripted.calls) == 4

    async def test_a_follow_up_pointing_back_never_uses_the_cache(
        self, api_client, db_session, model
    ):
        await a_chat_household(api_client, db_session, "+14165578804")
        scripted = model(
            *AUGUST_ANSWER,
            say("That was mostly groceries."),
            say("That was mostly groceries."),
        )
        conversation = await a_conversation(api_client)
        await ask(api_client, conversation, "What did I spend in August?")

        for _ in range(2):
            response = await ask(api_client, conversation, "What was that mostly?")
            assert response.json()["answered_by"] == "model"
        assert len(scripted.calls) == 4


class TestWhatIsSent:
    async def test_only_the_text_of_the_last_six_turns(
        self, api_client, db_session, model
    ):
        await a_chat_household(api_client, db_session, "+14165578811")
        conversation = await a_conversation(api_client)
        earlier = []
        for i in range(5):
            earlier += [
                {"role": "user", "text": f"question {i}", "at": "2026-09-14T10:00:00Z"},
                {
                    "role": "assistant",
                    "text": f"answer {i}",
                    "at": "2026-09-14T10:00:00Z",
                    "answered_by": "model",
                    "tools": [{"name": "spending_summary", "row_count": 1}],
                    "figures": [],
                },
            ]
        stored = await db_session.scalar(
            select(ChatConversation).where(
                ChatConversation.id == uuid.UUID(conversation)
            )
        )
        stored.messages = earlier
        await db_session.commit()
        scripted = model(say("Here you go."))

        await ask(api_client, conversation, "And now?")

        sent = scripted.calls[0]["messages"]
        assert [(m.role, m.text) for m in sent] == [
            ("user", "question 2"),
            ("assistant", "answer 2"),
            ("user", "question 3"),
            ("assistant", "answer 3"),
            ("user", "question 4"),
            ("assistant", "answer 4"),
            ("user", "And now?"),
        ], "the last six turns, as text, then the question"
        assert not any(m.tool_calls or m.tool_call_id for m in sent), "no tool traffic"

    async def test_the_system_prompt_is_the_same_on_every_call(
        self, api_client, db_session, model
    ):
        await a_chat_household(api_client, db_session, "+14165578812")
        scripted = model(*AUGUST_ANSWER)
        conversation = await a_conversation(api_client)

        await ask(api_client, conversation, "What did I spend in August?")

        first, second = scripted.calls
        assert first["system"] == second["system"]
        assert first["tools"] == second["tools"]
