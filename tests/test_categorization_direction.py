"""Which side of the ledger a category can be on (2026-11-05).

The model was sent unsigned amounts, so it could not tell interest charged
from interest earned: a card's interest was filed as Income and a savings
account's as Fees. Amounts now carry their sign, and money going out answered
as Income goes to a person instead of onto the income side.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.models.enums import TransactionDirection
from app.services.categorization import OTHER, categorize, signed
from tests.conftest import requires_db

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]


class Model:
    """Answers every pair with one slug, and keeps what it was sent."""

    model = "fake"

    def __init__(self, slug: str):
        self.slug = slug
        self.system = ""
        self.sent: list = []

    async def complete(self, *, system: str, user: str, max_output_tokens: int) -> str:
        self.system = system
        self.sent = json.loads(user)
        return json.dumps([self.slug] * len(self.sent))


def test_an_amount_carries_its_side():
    assert signed("70.40", TransactionDirection.debit) == "-70.40"
    assert signed("7.31", TransactionDirection.credit) == "+7.31"
    assert signed("-70.40", TransactionDirection.debit) == "-70.40", "never twice"


async def test_money_going_out_is_never_filed_as_income(db_session):
    model = Model("income")

    found = await categorize(
        db_session,
        model,
        household_id=uuid.uuid4(),
        pairs=[("PURCHASE INTEREST", "-70.40"), ("INTEREST PAID", "+7.31")],
    )

    assert found[0].slug == OTHER and not found[0].recognised, "to review"
    assert found[1].slug == "income" and found[1].recognised


async def test_the_model_is_told_what_the_signs_mean(db_session):
    model = Model("fees")

    await categorize(
        db_session,
        model,
        household_id=uuid.uuid4(),
        pairs=[("PURCHASE INTEREST", "-70.40")],
    )

    assert model.sent == [["PURCHASE INTEREST", "-70.40"]]
    assert '"-" is money going out' in model.system
    assert "debit" not in model.system.lower(), "no word for the account's side"


async def test_a_refund_may_still_land_in_a_spending_category(db_session):
    found = await categorize(
        db_session,
        Model("groceries"),
        household_id=uuid.uuid4(),
        pairs=[("LOBLAWS REFUND", "+12.00")],
    )

    assert found[0].slug == "groceries" and found[0].recognised
