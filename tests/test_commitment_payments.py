"""Ticking a commitment off by hand — with a transaction already recorded, or
one typed in there and then — end to end.

What matters most: a payment is counted once. Naming a recorded transaction
adds no spending; typing one in adds it once, in the commitment's line; and a
transaction never pays two commitments.

"Today" is pinned to 2026-09-15, as in `test_budgets_api`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.conftest import requires_db
from tests.test_budgets_api import SEPTEMBER, TODAY
from tests.test_dashboard import a_household, setup_wizard, tx

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

OBLIGATIONS = [
    {"name": "Rent", "monthly_amount": "1800.00", "category": "rent", "due_day": 1},
    {"name": "Gym", "monthly_amount": "40.00", "category": "subscriptions"},
]


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr("app.api.budgets._today", lambda: TODAY)


def commitments(body) -> dict[str, dict]:
    return {c["name"]: c for c in body["commitments"]}


async def a_household_with_rent(api_client, db_session, phone: str):
    """Rent paid in September as an e-transfer the matching cannot name."""
    household, account = await a_household(api_client, phone)
    await setup_wizard(api_client, obligations=OBLIGATIONS)
    transfer = tx(
        household,
        account,
        minor=180_000,
        month=9,
        day=2,
        description="E-TRANSFER J SMITH",
    )
    elsewhere = tx(household, account, minor=4_000, month=8, day=20, description="CASH")
    refund = tx(
        household,
        account,
        minor=4_000,
        month=9,
        day=3,
        credit=True,
        description="REFUND",
    )
    db_session.add_all([transfer, elsewhere, refund])
    await db_session.commit()
    body = (await api_client.get(SEPTEMBER)).json()
    return account, transfer.id, elsewhere.id, refund.id, body


class TestWithARecordedTransaction:
    async def test_it_ticks_off_and_adds_no_spending(self, api_client, db_session):
        _, transfer, _, _, before = await a_household_with_rent(
            api_client, db_session, "+14165579301"
        )
        rent = commitments(before)["Rent"]
        assert rent["paid_on"] is None, "the matching could not name it"

        response = await api_client.put(
            f"{SEPTEMBER}/commitments/{rent['id']}/payment",
            json={"transaction_id": str(transfer)},
        )

        assert response.status_code == 200, response.text
        after = commitments(response.json())["Rent"]
        assert after["paid_on"] == "2026-09-02"
        assert after["paid_amount"] == "1800.00"
        assert after["paid_by_hand"] is True
        assert after["transaction_id"] == str(transfer)
        assert response.json()["month_spending"] == before["month_spending"]

    async def test_one_payment_never_pays_two(self, api_client, db_session):
        _, transfer, _, _, before = await a_household_with_rent(
            api_client, db_session, "+14165579302"
        )
        rent, gym = commitments(before)["Rent"], commitments(before)["Gym"]
        await api_client.put(
            f"{SEPTEMBER}/commitments/{rent['id']}/payment",
            json={"transaction_id": str(transfer)},
        )

        response = await api_client.put(
            f"{SEPTEMBER}/commitments/{gym['id']}/payment",
            json={"transaction_id": str(transfer)},
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "already_pays_another_commitment"

    async def test_only_money_out_in_that_month(self, api_client, db_session):
        _, _, elsewhere, refund, before = await a_household_with_rent(
            api_client, db_session, "+14165579303"
        )
        gym = commitments(before)["Gym"]

        august = await api_client.put(
            f"{SEPTEMBER}/commitments/{gym['id']}/payment",
            json={"transaction_id": str(elsewhere)},
        )
        money_in = await api_client.put(
            f"{SEPTEMBER}/commitments/{gym['id']}/payment",
            json={"transaction_id": str(refund)},
        )

        assert august.status_code == 422
        assert august.json()["detail"]["code"] == "not_in_month"
        assert money_in.status_code == 422
        assert money_in.json()["detail"]["code"] == "not_spending"

    async def test_unticking_puts_it_back(self, api_client, db_session):
        _, transfer, _, _, before = await a_household_with_rent(
            api_client, db_session, "+14165579304"
        )
        rent = commitments(before)["Rent"]
        await api_client.put(
            f"{SEPTEMBER}/commitments/{rent['id']}/payment",
            json={"transaction_id": str(transfer)},
        )

        response = await api_client.delete(
            f"{SEPTEMBER}/commitments/{rent['id']}/payment"
        )

        assert response.status_code == 200
        assert commitments(response.json())["Rent"]["paid_on"] is None

    async def test_it_survives_setup_being_saved_again(self, api_client, db_session):
        _, transfer, _, _, before = await a_household_with_rent(
            api_client, db_session, "+14165579305"
        )
        rent = commitments(before)["Rent"]
        await api_client.put(
            f"{SEPTEMBER}/commitments/{rent['id']}/payment",
            json={"transaction_id": str(transfer)},
        )

        await setup_wizard(api_client, obligations=OBLIGATIONS)
        body = (await api_client.get(SEPTEMBER)).json()

        assert commitments(body)["Rent"]["paid_by_hand"] is True


class TestWithANewPayment:
    async def test_it_is_recorded_once_in_its_line(self, api_client, db_session):
        account, _, _, _, before = await a_household_with_rent(
            api_client, db_session, "+14165579311"
        )
        gym = commitments(before)["Gym"]

        response = await api_client.post(
            f"{SEPTEMBER}/commitments/{gym['id']}/payment",
            json={
                "account_id": str(account),
                "occurred_on": "2026-09-05",
                "amount": "40.00",
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert commitments(body)["Gym"]["paid_on"] == "2026-09-05"
        assert commitments(body)["Gym"]["paid_by_hand"] is True
        assert Decimal(body["month_spending"]) == Decimal(
            before["month_spending"]
        ) + Decimal("40.00")
        listed = (await api_client.get("/transactions?month=2026-09")).json()["rows"]
        gym_rows = [r for r in listed if r["description"] == "Gym"]
        assert len(gym_rows) == 1
        assert gym_rows[0]["direction"] == "debit"

    async def test_a_copy_of_a_recorded_row_is_refused_naming_it(
        self, api_client, db_session
    ):
        account, _, _, _, before = await a_household_with_rent(
            api_client, db_session, "+14165579312"
        )
        gym = commitments(before)["Gym"]
        payment = {
            "account_id": str(account),
            "occurred_on": "2026-09-05",
            "amount": "40.00",
        }
        await api_client.post(
            f"{SEPTEMBER}/commitments/{gym['id']}/payment", json=payment
        )
        await api_client.delete(f"{SEPTEMBER}/commitments/{gym['id']}/payment")

        again = await api_client.post(
            f"{SEPTEMBER}/commitments/{gym['id']}/payment", json=payment
        )

        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "duplicate_transaction"
        assert again.json()["detail"]["duplicate_of"]["amount"] == "40.00"

    async def test_a_date_outside_the_month_is_refused(self, api_client, db_session):
        account, _, _, _, before = await a_household_with_rent(
            api_client, db_session, "+14165579313"
        )
        gym = commitments(before)["Gym"]

        response = await api_client.post(
            f"{SEPTEMBER}/commitments/{gym['id']}/payment",
            json={
                "account_id": str(account),
                "occurred_on": "2026-08-30",
                "amount": "40.00",
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "not_in_month"

    async def test_someone_elses_commitment_is_not_found(self, api_client, db_session):
        account, _, _, _, before = await a_household_with_rent(
            api_client, db_session, "+14165579314"
        )
        gym = commitments(before)["Gym"]
        await a_household(api_client, "+14165579315")

        response = await api_client.post(
            f"{SEPTEMBER}/commitments/{gym['id']}/payment",
            json={
                "account_id": str(account),
                "occurred_on": "2026-09-05",
                "amount": "40.00",
            },
        )

        assert response.status_code == 404
