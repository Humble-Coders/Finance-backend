"""Debts kept current for the payoff plan (#76): `/debts`, the wizard sharing
its rows, and a statement's confirmed closing balance."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.enums import BalanceSource, SourceKind, StatementImportStatus
from app.models.money import StatementImport
from app.models.planning import Debt
from tests.conftest import requires_db
from tests.test_dashboard import a_household, setup_wizard

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

TODAY = datetime.now(UTC).date()
VISA = {
    "name": "Visa",
    "balance": "4000.00",
    "minimum_payment": "120.00",
    "interest_rate_percent": "20.99",
}
CAR = {
    "name": "Car loan",
    "balance": "12000.00",
    "minimum_payment": "350.00",
    "interest_rate_percent": "6.90",
}


async def debts(api_client) -> list[dict]:
    response = await api_client.get("/debts")
    assert response.status_code == 200, response.text
    return response.json()["debts"]


async def a_card(api_client, name: str = "Visa card") -> str:
    created = await api_client.post(
        "/accounts", json={"name": name, "kind": "credit_card"}
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


class TestTheWizardsDebts:
    async def test_they_show_with_their_defaults(self, api_client):
        await a_household(api_client, "+14165576101")
        await setup_wizard(api_client, debts=[VISA, CAR])

        listed = await debts(api_client)

        assert [d["name"] for d in listed] == ["Visa", "Car loan"]
        visa = listed[0]
        assert (visa["kind"], visa["balance_source"], visa["from_setup"]) == (
            "other",
            "setup",
            True,
        )
        assert visa["interest_rate_percent"] == "20.99"
        assert visa["balance_as_of"] == TODAY.isoformat()
        assert visa["balance_stale"] is False

    async def test_a_wizard_save_keeps_what_it_does_not_ask(self, api_client):
        """Type, promotion, account link and a balance set elsewhere survive the
        wizard re-sending what it shows."""
        await a_household(api_client, "+14165576102")
        await setup_wizard(api_client, debts=[VISA, CAR])
        card = await a_card(api_client)
        visa_id = (await debts(api_client))[0]["id"]
        edited = await api_client.patch(
            f"/debts/{visa_id}",
            json={
                "kind": "credit_card",
                "account_id": card,
                "promo_rate_percent": "0",
                "promo_ends_on": (TODAY + timedelta(days=90)).isoformat(),
                "balance": "3800.00",
            },
        )
        assert edited.status_code == 200, edited.text
        shown = (await api_client.get("/financial-setup")).json()

        await setup_wizard(api_client, debts=shown["debts"])

        visa = (await debts(api_client))[0]
        assert visa["id"] == visa_id, "updated in place, not recreated"
        assert (visa["kind"], visa["account_id"], visa["promo_rate_percent"]) == (
            "credit_card",
            card,
            "0.00",
        )
        assert (visa["balance"], visa["balance_source"]) == ("3800.00", "manual")

    async def test_a_balance_changed_in_the_wizard_is_the_wizards(self, api_client):
        await a_household(api_client, "+14165576103")
        await setup_wizard(api_client, debts=[VISA])
        visa_id = (await debts(api_client))[0]["id"]
        await api_client.patch(f"/debts/{visa_id}", json={"balance": "3900.00"})

        await setup_wizard(api_client, debts=[{**VISA, "balance": "3500.00"}])

        visa = (await debts(api_client))[0]
        assert (visa["balance"], visa["balance_source"]) == ("3500.00", "setup")

    async def test_rows_removed_in_the_wizard_go_and_debts_added_elsewhere_stay(
        self, api_client
    ):
        await a_household(api_client, "+14165576104")
        await setup_wizard(api_client, debts=[VISA, CAR])
        added = await api_client.post(
            "/debts", json={"name": "Line of credit", "balance": "2000.00"}
        )
        assert added.status_code == 201, added.text

        await setup_wizard(api_client, debts=[VISA])

        assert [d["name"] for d in await debts(api_client)] == [
            "Visa",
            "Line of credit",
        ]


class TestEditing:
    async def test_add_a_debt_with_a_promotion(self, api_client):
        await a_household(api_client, "+14165576110")

        created = await api_client.post(
            "/debts",
            json={
                "name": "Balance transfer",
                "kind": "balance_transfer",
                "balance": "2500.00",
                "minimum_payment": "50.00",
                "interest_rate_percent": "22.99",
                "promo_rate_percent": "0",
                "promo_ends_on": (TODAY + timedelta(days=180)).isoformat(),
            },
        )

        assert created.status_code == 201, created.text
        body = created.json()
        assert (body["kind"], body["promo_rate_percent"], body["balance_source"]) == (
            "balance_transfer",
            "0.00",
            "manual",
        )
        assert body["from_setup"] is False

    @pytest.mark.parametrize(
        ("phone", "change", "field"),
        [
            ("+14165576121", {"promo_rate_percent": "0"}, "promo_ends_on"),
            ("+14165576122", {"promo_ends_on": "2027-03-01"}, "promo_rate_percent"),
            ("+14165576123", {"balance": "-10.00"}, "balance"),
            ("+14165576124", {"interest_rate_percent": "120"}, "interest_rate_percent"),
            ("+14165576125", {"balance": "12.345"}, "balance"),
        ],
    )
    async def test_a_value_it_cannot_store_is_refused_by_name(
        self, api_client, phone, change, field
    ):
        await a_household(api_client, phone)
        created = await api_client.post(
            "/debts", json={"name": "Visa", "balance": "100.00"}
        )
        response = await api_client.patch(f"/debts/{created.json()['id']}", json=change)

        assert response.status_code == 422, response.text
        assert response.json()["detail"]["field"] == field

    async def test_update_balance_dates_it_today_and_an_unchanged_one_keeps_its_date(
        self, api_client, db_session
    ):
        await a_household(api_client, "+14165576130")
        await setup_wizard(api_client, debts=[VISA])
        visa_id = (await debts(api_client))[0]["id"]
        row = await db_session.get(Debt, uuid.UUID(visa_id))
        row.balance_as_of = TODAY - timedelta(days=60)
        await db_session.commit()
        assert (await debts(api_client))[0]["balance_stale"] is True

        same = await api_client.patch(f"/debts/{visa_id}", json={"balance": "4000.00"})
        assert same.json()["balance_as_of"] == (TODAY - timedelta(days=60)).isoformat()

        updated = await api_client.patch(
            f"/debts/{visa_id}", json={"balance": "3700.00"}
        )

        assert (updated.json()["balance_as_of"], updated.json()["balance_source"]) == (
            TODAY.isoformat(),
            "manual",
        )
        assert updated.json()["balance_stale"] is False

    async def test_name_and_balance_cannot_be_cleared_but_the_minimum_can(
        self, api_client
    ):
        await a_household(api_client, "+14165576131")
        created = (await api_client.post("/debts", json={**VISA})).json()

        assert (
            await api_client.patch(f"/debts/{created['id']}", json={"name": None})
        ).status_code == 422
        cleared = await api_client.patch(
            f"/debts/{created['id']}", json={"minimum_payment": None}
        )
        assert cleared.json()["minimum_payment"] is None

    async def test_delete(self, api_client):
        await a_household(api_client, "+14165576132")
        created = (await api_client.post("/debts", json={**VISA})).json()

        assert (await api_client.delete(f"/debts/{created['id']}")).status_code == 204
        assert (await api_client.delete(f"/debts/{created['id']}")).status_code == 404


class TestLinking:
    async def test_only_the_households_own_card_or_loan(self, api_client):
        _, chequing = await a_household(api_client, "+14165576140")
        card = await a_card(api_client)
        visa = (await api_client.post("/debts", json={**VISA})).json()

        linked = await api_client.patch(
            f"/debts/{visa['id']}", json={"account_id": card}
        )
        to_chequing = await api_client.patch(
            f"/debts/{visa['id']}", json={"account_id": str(chequing)}
        )
        to_nobody = await api_client.patch(
            f"/debts/{visa['id']}", json={"account_id": str(uuid.uuid4())}
        )
        unlinked = await api_client.patch(
            f"/debts/{visa['id']}", json={"account_id": None}
        )

        assert linked.json()["account_id"] == card
        assert to_chequing.status_code == 404
        assert to_chequing.json()["detail"]["code"] == "unusable_account"
        assert to_nobody.status_code == 404
        assert unlinked.json()["account_id"] is None

    async def test_another_households_debt_is_not_found(self, api_client):
        await a_household(api_client, "+14165576141")
        theirs = (await api_client.post("/debts", json={**VISA})).json()
        await a_household(api_client, "+14165576142")

        assert (
            await api_client.patch(f"/debts/{theirs['id']}", json={"name": "Mine"})
        ).status_code == 404
        assert (await api_client.delete(f"/debts/{theirs['id']}")).status_code == 404
        assert await debts(api_client) == []


class TestTheStatementsBalance:
    async def a_linked_card(self, api_client, phone: str) -> tuple[str, str, str]:
        household, _ = await a_household(api_client, phone)
        card = await a_card(api_client)
        visa = (
            await api_client.post("/debts", json={**VISA, "account_id": card})
        ).json()
        return str(household), card, visa["id"]

    async def save(
        self, api_client, db_session, household: str, account: str, **closing
    ):
        record = StatementImport(
            household_id=uuid.UUID(household),
            source_kind=SourceKind.pdf_text,
            status=StatementImportStatus.awaiting_review,
        )
        db_session.add(record)
        await db_session.flush()
        row = {
            "occurred_on": (TODAY - timedelta(days=3)).isoformat(),
            "description": "LOBLAWS",
            "amount": "40.00",
            "direction": "debit",
            "confidence": 95,
        }
        return await api_client.post(
            f"/statements/{record.id}/transactions",
            json={"account_id": account, "rows": [row], "categorised": True, **closing},
        )

    async def test_a_confirmed_balance_updates_the_linked_debt(
        self, api_client, db_session
    ):
        household, card, visa = await self.a_linked_card(api_client, "+14165576150")
        await db_session.execute(
            Debt.__table__.update()
            .where(Debt.id == uuid.UUID(visa))
            .values(balance_as_of=TODAY - timedelta(days=20))
        )
        await db_session.commit()

        response = await self.save(
            api_client,
            db_session,
            household,
            card,
            closing_balance="3612.48",
            closing_date=(TODAY - timedelta(days=1)).isoformat(),
        )

        assert response.status_code == 200, response.text
        assert response.json()["debts_updated"] == 1
        debt = next(d for d in await debts(api_client) if d["id"] == visa)
        assert (debt["balance"], debt["balance_source"]) == ("3612.48", "statement")
        assert debt["balance_as_of"] == (TODAY - timedelta(days=1)).isoformat()
        assert debt["balance_stale"] is False

    async def test_an_older_statement_never_overwrites_a_newer_balance(
        self, api_client, db_session
    ):
        household, card, visa = await self.a_linked_card(api_client, "+14165576151")

        response = await self.save(
            api_client,
            db_session,
            household,
            card,
            closing_balance="9999.00",
            closing_date=(TODAY - timedelta(days=40)).isoformat(),
        )

        assert response.json()["debts_updated"] == 0
        debt = next(d for d in await debts(api_client) if d["id"] == visa)
        assert debt["balance"] == "4000.00"

    async def test_no_balance_changes_nothing(self, api_client, db_session):
        household, card, visa = await self.a_linked_card(api_client, "+14165576152")

        response = await self.save(api_client, db_session, household, card)

        assert response.status_code == 200
        assert response.json()["debts_updated"] == 0

    @pytest.mark.parametrize(
        ("phone", "closing", "field"),
        [
            ("+14165576161", {"closing_balance": "100.00"}, "closing_date"),
            ("+14165576162", {"closing_date": "2026-10-01"}, "closing_balance"),
            (
                "+14165576163",
                {"closing_balance": "-5.00", "closing_date": "2026-10-01"},
                "closing_balance",
            ),
        ],
    )
    async def test_a_half_or_negative_balance_is_refused_before_saving(
        self, api_client, db_session, phone, closing, field
    ):
        household, card, _ = await self.a_linked_card(api_client, phone)

        response = await self.save(api_client, db_session, household, card, **closing)

        assert response.status_code == 422, response.text
        assert response.json()["detail"]["field"] == field
        saved = await db_session.scalar(
            select(StatementImport.closing_balance_minor_units).where(
                StatementImport.household_id == uuid.UUID(household),
                StatementImport.closing_balance_minor_units.is_not(None),
            )
        )
        assert saved is None

    async def test_only_debts_linked_to_that_account(self, api_client, db_session):
        household, card, visa = await self.a_linked_card(api_client, "+14165576170")
        other = (await api_client.post("/debts", json={**CAR})).json()

        await self.save(
            api_client,
            db_session,
            household,
            card,
            closing_balance="3500.00",
            closing_date=TODAY.isoformat(),
        )

        car = next(d for d in await debts(api_client) if d["id"] == other["id"])
        assert car["balance"] == "12000.00"
        assert BalanceSource.statement.value != car["balance_source"]
