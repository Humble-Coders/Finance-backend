"""Monthly totals (#83): equal to the dashboard, and current after every write.

Two promises. Each month's totals are the dashboard's figures to the minor
unit — on ten years of data, with one-offs, other currencies and suspected
duplicates mixed in. And after every way the ledger is written, the totals
equal a rebuild from nothing, and the ledger version went up.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.derived import MonthlyCategoryTotal
from app.models.enums import ReviewReason, SourceKind, StatementImportStatus
from app.models.money import StatementImport, Transaction
from app.scripts.rebuild_rollups import rebuild_all
from app.services import filing, rollups
from app.services.dashboard import NOT_A_FLOW
from tests.conftest import requires_db
from tests.fixtures.long_history import MONTHS, ten_years
from tests.test_budgets_api import SEPTEMBER, TODAY
from tests.test_dashboard import a_household, a_system_category_id, setup_wizard, tx
from tests.test_transfers import Categorizer
from tests.test_transfers import a_household as a_household_with_accounts

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

T = MonthlyCategoryTotal


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr("app.api.budgets._today", lambda: TODAY)


@pytest.fixture
def categorizer(monkeypatch):
    monkeypatch.setattr(filing, "build_client", lambda _s: Categorizer())


async def snapshot(db_session, household_id) -> list[tuple]:
    result = await db_session.execute(
        select(
            T.currency,
            T.month,
            T.category_id,
            T.category_slug,
            T.direction,
            T.total_minor_units,
            T.one_off_minor_units,
            T.transaction_count,
        ).where(T.household_id == household_id)
    )
    return sorted(result.all(), key=str)


async def assert_current(db_session, household_id, version_before: int) -> None:
    """The totals as the write left them equal a rebuild from nothing, and the
    version moved."""
    assert await rollups.ledger_version(db_session, household_id) > version_before
    kept = await snapshot(db_session, household_id)
    await rollups.rebuild(db_session, household_id)
    assert await snapshot(db_session, household_id) == kept


async def month_total(db_session, household_id, month: date, slug: str) -> int:
    return int(
        await db_session.scalar(
            select(func.coalesce(func.sum(T.total_minor_units), 0)).where(
                T.household_id == household_id,
                T.month == month,
                T.category_slug == slug,
                T.currency == "CAD",
            )
        )
    )


def cents(amount: str) -> int:
    return int(Decimal(amount) * 100)


# --- Equal to the dashboard ----------------------------------------------------------


class TestTenYearsAgreeWithTheDashboard:
    async def test_every_month_to_the_minor_unit(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165578301")
        history = await ten_years(db_session, household, account)
        assert history.rows > 35_000

        flows = (
            select(T.month, T.direction, func.sum(T.total_minor_units))
            .where(
                T.household_id == household,
                T.currency == "CAD",
                (T.category_slug.is_(None)) | (T.category_slug.not_in(NOT_A_FLOW)),
            )
            .group_by(T.month, T.direction)
        )
        totals = {
            (month, direction): int(amount)
            for month, direction, amount in (await db_session.execute(flows)).all()
        }
        for month in MONTHS:
            body = (await api_client.get(f"/dashboard?month={month:%Y-%m}")).json()
            assert cents(body["expenses"]["actual"]) == totals[(month, "debit")], month
            assert cents(body["income"]["actual"]) == totals[(month, "credit")], month

    async def test_other_currencies_and_duplicates_are_in_no_total(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165578302")
        await ten_years(db_session, household, account)

        usd = await db_session.scalar(
            select(func.sum(T.transaction_count)).where(
                T.household_id == household, T.currency == "USD"
            )
        )
        assert usd == 240, "kept apart, for a reader to leave out"
        duplicates = await db_session.scalar(
            select(func.count(Transaction.id)).where(
                Transaction.household_id == household,
                Transaction.review_reason == ReviewReason.suspected_duplicate,
            )
        )
        counted = await db_session.scalar(
            select(func.sum(T.transaction_count)).where(T.household_id == household)
        )
        rows = await db_session.scalar(
            select(func.count(Transaction.id)).where(
                Transaction.household_id == household
            )
        )
        assert counted == rows - duplicates

    async def test_one_offs_are_in_the_total_and_apart(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165578303")
        await ten_years(db_session, household, account)

        january = await db_session.execute(
            select(T.total_minor_units, T.one_off_minor_units).where(
                T.household_id == household,
                T.month == date(2020, 1, 1),
                T.category_slug == "shopping",
                T.currency == "CAD",
            )
        )
        total, one_off = january.one()
        assert one_off == 150_000
        assert total > one_off, "the laptop is inside the month's shopping"


# --- Current after every write -------------------------------------------------------


async def a_household_with_rows(api_client, db_session, phone: str):
    household, account = await a_household(api_client, phone)
    await setup_wizard(api_client)
    groceries = await a_system_category_id(db_session, "groceries")
    rows = [
        tx(household, account, minor=5_000, month=7, day=3, category_id=groceries),
        tx(household, account, minor=7_000, month=8, day=4, category_id=groceries),
        tx(
            household,
            account,
            minor=7_000,
            month=8,
            day=6,
            category_id=groceries,
            needs_review=True,
            reason=ReviewReason.suspected_duplicate,
            description="SOMETHING ELSE",
        ),
    ]
    db_session.add_all(rows)
    await db_session.flush()
    await rollups.rebuild(db_session, household)
    await db_session.commit()
    version = await rollups.ledger_version(db_session, household)
    return household, account, groceries, rows, version


class TestEveryWritePathKeepsThemCurrent:
    async def test_adding_by_hand(self, api_client, db_session):
        household, account, groceries, _, version = await a_household_with_rows(
            api_client, db_session, "+14165578311"
        )

        response = await api_client.post(
            "/transactions",
            json={
                "account_id": str(account),
                "occurred_on": "2026-08-20",
                "amount": "12.34",
                "direction": "debit",
                "description": "METRO",
                "category_id": str(groceries),
            },
        )

        assert response.status_code == 201, response.text
        assert (
            await month_total(db_session, household, date(2026, 8, 1), "groceries")
            == 8_234
        )
        await assert_current(db_session, household, version)

    async def test_moving_a_date_rebuilds_both_months(self, api_client, db_session):
        household, _, _, rows, version = await a_household_with_rows(
            api_client, db_session, "+14165578312"
        )

        response = await api_client.patch(
            f"/transactions/{rows[1].id}", json={"occurred_on": "2026-07-20"}
        )

        assert response.status_code == 200, response.text
        assert (
            await month_total(db_session, household, date(2026, 7, 1), "groceries")
            == 12_000
        )
        assert (
            await month_total(db_session, household, date(2026, 8, 1), "groceries") == 0
        )
        await assert_current(db_session, household, version)

    async def test_a_correction_and_the_rows_its_rule_refiles(
        self, api_client, db_session
    ):
        household, account, groceries, _, _ = await a_household_with_rows(
            api_client, db_session, "+14165578313"
        )
        dining = await a_system_category_id(db_session, "dining")
        # Two rows from one merchant waiting for review, in different months:
        # correcting one teaches a rule that re-files the other.
        waiting = [
            tx(
                household,
                account,
                minor=1_500,
                month=6,
                day=9,
                description="CAFE X",
                category_id=groceries,
                needs_review=True,
                reason=ReviewReason.low_confidence,
            ),
            tx(
                household,
                account,
                minor=2_500,
                month=8,
                day=9,
                description="CAFE X",
                category_id=groceries,
                needs_review=True,
                reason=ReviewReason.low_confidence,
            ),
        ]
        db_session.add_all(waiting)
        await db_session.flush()
        await rollups.rebuild(db_session, household)
        await db_session.commit()
        version = await rollups.ledger_version(db_session, household)

        response = await api_client.patch(
            f"/transactions/{waiting[1].id}", json={"category_id": str(dining)}
        )

        assert response.status_code == 200, response.text
        assert response.json()["recategorized"] == 1
        assert (
            await month_total(db_session, household, date(2026, 6, 1), "dining")
            == 1_500
        )
        await assert_current(db_session, household, version)

    async def test_marking_a_one_off(self, api_client, db_session):
        household, _, _, rows, version = await a_household_with_rows(
            api_client, db_session, "+14165578314"
        )

        response = await api_client.put(
            f"/transactions/{rows[1].id}/one-off", json={"one_off": True}
        )

        assert response.status_code == 200, response.text
        one_off = await db_session.scalar(
            select(T.one_off_minor_units).where(
                T.household_id == household,
                T.month == date(2026, 8, 1),
                T.category_slug == "groceries",
            )
        )
        assert one_off == 7_000
        await assert_current(db_session, household, version)

    async def test_confirming_a_suspected_duplicate_starts_it_counting(
        self, api_client, db_session
    ):
        household, _, _, rows, version = await a_household_with_rows(
            api_client, db_session, "+14165578315"
        )

        response = await api_client.post(f"/transactions/{rows[2].id}/confirm")

        assert response.status_code == 200, response.text
        assert (
            await month_total(db_session, household, date(2026, 8, 1), "groceries")
            == 14_000
        )
        await assert_current(db_session, household, version)

    async def test_confirming_in_bulk(self, api_client, db_session):
        household, _, _, rows, version = await a_household_with_rows(
            api_client, db_session, "+14165578316"
        )

        response = await api_client.post(
            "/transactions/confirm", json={"ids": [str(rows[2].id)]}
        )

        assert response.status_code == 200, response.text
        assert (
            await month_total(db_session, household, date(2026, 8, 1), "groceries")
            == 14_000
        )
        await assert_current(db_session, household, version)

    async def test_deleting(self, api_client, db_session):
        household, _, _, rows, version = await a_household_with_rows(
            api_client, db_session, "+14165578317"
        )

        response = await api_client.delete(f"/transactions/{rows[0].id}")

        assert response.status_code == 200, response.text
        assert (
            await month_total(db_session, household, date(2026, 7, 1), "groceries") == 0
        )
        await assert_current(db_session, household, version)

    async def test_a_commitment_payment_typed_in(self, api_client, db_session):
        household, account, _, _, version = await a_household_with_rows(
            api_client, db_session, "+14165578318"
        )
        await setup_wizard(
            api_client,
            obligations=[
                {"name": "Gym", "monthly_amount": "40.00", "category": "subscriptions"}
            ],
        )
        budget = (await api_client.get(SEPTEMBER)).json()
        gym = next(c for c in budget["commitments"] if c["name"] == "Gym")

        response = await api_client.post(
            f"{SEPTEMBER}/commitments/{gym['id']}/payment",
            json={
                "account_id": str(account),
                "occurred_on": "2026-09-05",
                "amount": "40.00",
            },
        )

        assert response.status_code == 200, response.text
        september = await db_session.scalar(
            select(func.sum(T.total_minor_units)).where(
                T.household_id == household, T.month == date(2026, 9, 1)
            )
        )
        assert september == 4_000
        await assert_current(db_session, household, version)

    async def test_an_import_and_the_earlier_row_its_pairing_refiles(
        self, api_client, db_session, categorizer
    ):
        """The bank paid the card on July 31, and that statement was imported
        first: spending, as debt payment. The card's statement, imported
        later, shows the payment arriving on August 2 — and pairing re-files
        the July row as a transfer, in a month the card's statement has no
        rows in."""
        accounts = await a_household_with_accounts(api_client, "+14165578319")
        household = uuid.UUID(accounts["household"])

        async def imported(account: str, rows: list[dict]) -> dict:
            record = StatementImport(
                household_id=household,
                source_kind=SourceKind.pdf_text,
                status=StatementImportStatus.awaiting_review,
            )
            db_session.add(record)
            await db_session.flush()
            response = await api_client.post(
                f"/statements/{record.id}/transactions",
                json={"account_id": account, "rows": rows},
            )
            assert response.status_code == 200, response.text
            return response.json()

        def row(day: str, description: str, *, credit: bool) -> dict:
            return {
                "occurred_on": day,
                "description": description,
                "amount": "250.00",
                "direction": "credit" if credit else "debit",
                "confidence": 95,
            }

        await imported(
            accounts["chequing"], [row("2026-07-31", "VISA PAYMENT", credit=False)]
        )
        july = date(2026, 7, 1)
        assert await month_total(db_session, household, july, "debt_payment") == 25_000
        version = await rollups.ledger_version(db_session, household)

        saved = await imported(
            accounts["card"], [row("2026-08-02", "PAYMENT THANK YOU", credit=True)]
        )

        assert saved["earlier_needs_review"] == 1, "the July row was re-filed"
        assert await month_total(db_session, household, july, "debt_payment") == 0
        assert await month_total(db_session, household, july, "transfers") == 25_000
        await assert_current(db_session, household, version)


class TestTheVersion:
    async def test_it_starts_at_zero_and_counts_writes(self, api_client, db_session):
        household, _, _, rows, version = await a_household_with_rows(
            api_client, db_session, "+14165578321"
        )
        assert version == 1, "the rebuild that set the household up"

        await api_client.put(
            f"/transactions/{rows[1].id}/one-off", json={"one_off": True}
        )
        await api_client.put(
            f"/transactions/{rows[1].id}/one-off", json={"one_off": False}
        )

        assert await rollups.ledger_version(db_session, household) == 3

    async def test_a_household_never_written_is_at_zero(self, api_client, db_session):
        household, _ = await a_household(api_client, "+14165578322")

        assert await rollups.ledger_version(db_session, household) == 0

    async def test_no_dates_changes_nothing(self, api_client, db_session):
        household, _, _, _, version = await a_household_with_rows(
            api_client, db_session, "+14165578323"
        )

        await rollups.refresh(db_session, household, [None])

        assert await rollups.ledger_version(db_session, household) == version


class TestTheBackfill:
    async def test_the_script_rebuilds_a_household_with_no_totals(
        self, api_client, db_session, monkeypatch
    ):
        household, account = await a_household(api_client, "+14165578331")
        db_session.add(tx(household, account, minor=5_000, month=8, day=3))
        await db_session.commit()
        assert await snapshot(db_session, household) == [], "imported before #83"

        # The script opens its own sessions; here they are the test's, so its
        # work is rolled back with everything else.
        class Sessions:
            def __call__(self):
                return self

            async def __aenter__(self):
                return db_session

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(
            "app.scripts.rebuild_rollups.get_sessionmaker", lambda: Sessions()
        )

        households, written = await rebuild_all()

        assert households >= 1 and written >= 1
        assert len(await snapshot(db_session, household)) == 1
