"""What the AI coach's chat tests share (#71): a scripted model in place of
a real one, the fixtures that install it, and the household and request
helpers. `model` and `no_model` are registered in `conftest.py`."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app.services import chat as chat_service
from app.services import rollups
from app.services.llm import AssistantTurn, ToolCall, Usage
from tests.test_budgets_api import a_ready_household
from tests.test_statements_endpoint import consent_to_ai

CONVERSATIONS = "/chat/conversations"


# --- The scripted model -----------------------------------------------------------


class Scripted:
    """Plays [steps] in order: an AssistantTurn, or a function of the
    messages so far that returns one. Records every call."""

    model = "scripted"

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls: list[dict] = []
        self.usage = Usage()

    async def converse(self, *, system, messages, tools, max_output_tokens):
        self.calls.append(
            {
                "system": system,
                "messages": list(messages),
                "tools": [t.name for t in tools],
                "max_output_tokens": max_output_tokens,
            }
        )
        self.usage.add(
            input_tokens=300, output_tokens=40, reasoning_tokens=0, cost=None
        )
        if not self.steps:
            raise AssertionError("the model was asked more than the script holds")
        step = self.steps.pop(0)
        return step(messages) if callable(step) else step

    async def complete(self, **_):
        raise AssertionError("chat never calls complete()")

    async def aclose(self):
        pass


def call(name: str, **arguments) -> AssistantTurn:
    return AssistantTurn(None, (ToolCall(f"c-{name}", name, arguments),))


def say(reply: str) -> AssistantTurn:
    return AssistantTurn(reply)


def say_from(build):
    """A reply built from the last tool result the model was given."""

    def step(messages):
        last = next(m for m in reversed(messages) if m.role == "tool")
        return AssistantTurn(build(json.loads(last.text)))

    return step


@pytest.fixture
def model(monkeypatch):
    """`model(*steps)` installs a scripted model and returns it."""

    def install(*steps) -> Scripted:
        scripted = Scripted(*steps)
        monkeypatch.setattr(chat_service, "build_client", lambda _s: scripted)
        return scripted

    return install


@pytest.fixture
def no_model(monkeypatch):
    def refuse(_settings):
        raise AssertionError("a model client was built")

    monkeypatch.setattr(chat_service, "build_client", refuse)


# --- Households -------------------------------------------------------------------


async def chat_on(db_session) -> None:
    await db_session.execute(
        text(
            "UPDATE feature_availability SET is_enabled = true "
            "WHERE feature_key = 'ai_chat'"
        )
    )


async def a_chat_household(api_client, db_session, phone: str, *, consent=True):
    household, account, ids = await a_ready_household(api_client, db_session, phone)
    await rollups.rebuild(db_session, household)
    await chat_on(db_session)
    await db_session.commit()
    if consent:
        await consent_to_ai(api_client)
    return household


async def a_conversation(api_client) -> str:
    response = await api_client.post(CONVERSATIONS)
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def ask(api_client, conversation: str, question: str = "", **body):
    return await api_client.post(
        f"{CONVERSATIONS}/{conversation}/messages", json={"text": question, **body}
    )


AUGUST = dict(from_month="2026-08", to_month="2026-08", group_by="none")


async def a_learning_household(
    api_client, db_session, phone: str, *, rows: int = 5, consent: bool = True
):
    """A household still learning: [rows] transactions, all in September
    2026 — the running month, so no complete month yet."""
    from tests.test_dashboard import a_household, setup_wizard, tx

    household, account = await a_household(api_client, phone)
    await setup_wizard(api_client)
    db_session.add_all(
        tx(
            household,
            account,
            minor=1_000 + i,
            month=9,
            day=1 + i % 14,
            description=f"SHOP {i}",
        )
        for i in range(rows)
    )
    await db_session.flush()
    await rollups.rebuild(db_session, household)
    await chat_on(db_session)
    await db_session.commit()
    if consent:
        await consent_to_ai(api_client)
    return household, account
