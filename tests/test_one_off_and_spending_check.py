"""One-off purchases, budget lines changed for one month, and the spending
check — end to end, with a fake model.

What matters most: a purchase marked one-off never raises the months after,
even in a household's first months; a line changed for this month only leaves
the months after where they were; and the spending check's figures are the
server's, whatever the model says.

"Today" is pinned to 2026-09-15, as in `test_budgets_api`.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.api import ai_budget as endpoint
from app.models.money import Transaction
from app.services import spending_check
from tests.conftest import requires_db
from tests.test_ai_budget_api import FakeModel, use_model
from tests.test_budgets_api import (
    AUGUST,
    SEPTEMBER,
    TODAY,
    a_ready_household,
    by_slug,
    filler,
)
from tests.test_dashboard import a_household, a_system_category_id, setup_wizard, tx
from tests.test_statements_endpoint import consent_to_ai

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

JULY = "/budgets/2026-07"
CHECK = f"{SEPTEMBER}/spending-check"


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr("app.api.budgets._today", lambda: TODAY)


async def a_new_household_with_a_laptop(
    api_client, db_session, phone: str, *, marked: bool
):
    """August is the household's first month: shopping $200, and a $1,200
    laptop — one-off when [marked]."""
    household, account = await a_household(api_client, phone)
    await setup_wizard(api_client, income="5000.00")
    shopping = await a_system_category_id(db_session, "shopping")
    laptop = tx(
        household,
        account,
        minor=120_000,
        month=8,
        day=20,
        category_id=shopping,
        description="BEST BUY LAPTOP",
    )
    laptop.is_one_off = marked
    db_session.add_all(
        [
            *filler(household, account, 20, month=8),
            tx(
                household,
                account,
                minor=20_000,
                month=8,
                day=5,
                category_id=shopping,
                description="WINNERS",
            ),
            laptop,
        ]
    )
    await db_session.commit()
    return laptop.id


class TestAOneOffPurchase:
    async def test_it_never_raises_the_months_after(self, api_client, db_session):
        await a_new_household_with_a_laptop(
            api_client, db_session, "+14165579001", marked=True
        )

        lines = by_slug((await api_client.get(SEPTEMBER)).json())

        assert lines["shopping"]["allocated"] == "200.00", "the laptop left out"

    async def test_unmarked_it_would_have(self, api_client, db_session):
        await a_new_household_with_a_laptop(
            api_client, db_session, "+14165579002", marked=False
        )

        lines = by_slug((await api_client.get(SEPTEMBER)).json())

        assert lines["shopping"]["allocated"] == "1400.00"

    async def test_it_still_counts_in_its_own_month(self, api_client, db_session):
        await a_new_household_with_a_laptop(
            api_client, db_session, "+14165579003", marked=True
        )

        body = (await api_client.get(AUGUST)).json()

        shopping = next(u for u in body["unbudgeted"] if u["slug"] == "shopping")
        assert shopping["spent"] == "1400.00"
        assert shopping["one_off"] == "1200.00"

    async def test_marking_it_by_hand(self, api_client, db_session):
        laptop = await a_new_household_with_a_laptop(
            api_client, db_session, "+14165579004", marked=False
        )

        response = await api_client.put(
            f"/transactions/{laptop}/one-off", json={"one_off": True}
        )

        assert response.status_code == 200, response.text
        assert response.json()["one_off"] is True
        listed = (await api_client.get("/transactions?month=2026-08")).json()["rows"]
        assert next(r for r in listed if r["id"] == str(laptop))["one_off"] is True
        lines = by_slug((await api_client.get(SEPTEMBER)).json())
        assert lines["shopping"]["allocated"] == "200.00"

    async def test_marking_it_leaves_its_review_alone(self, api_client, db_session):
        laptop = await a_new_household_with_a_laptop(
            api_client, db_session, "+14165579005", marked=False
        )
        row = await db_session.get(Transaction, laptop)
        row.needs_review = True
        await db_session.commit()

        await api_client.put(f"/transactions/{laptop}/one-off", json={"one_off": True})

        db_session.expire_all()
        assert (await db_session.get(Transaction, laptop)).needs_review is True

    async def test_money_in_is_never_a_one_off(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165579006")
        refund = tx(household, account, minor=5_000, credit=True, description="REFUND")
        db_session.add(refund)
        await db_session.commit()

        response = await api_client.put(
            f"/transactions/{refund.id}/one-off", json={"one_off": True}
        )

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "not_spending"

    async def test_someone_elses_row_is_not_found(self, api_client, db_session):
        laptop = await a_new_household_with_a_laptop(
            api_client, db_session, "+14165579007", marked=False
        )
        await a_household(api_client, "+14165579008")

        response = await api_client.put(
            f"/transactions/{laptop}/one-off", json={"one_off": True}
        )

        assert response.status_code == 404


class TestThisMonthOnly:
    async def test_the_months_after_keep_what_it_carried(self, api_client, db_session):
        _, _, ids = await a_ready_household(api_client, db_session, "+14165579101")
        await api_client.put(f"{JULY}/lines/{ids['dining']}", json={"amount": "60.00"})
        assert (
            by_slug((await api_client.get(AUGUST)).json())["dining"]["allocated"]
            == "60.00"
        )

        put = await api_client.put(
            f"{AUGUST}/lines/{ids['dining']}",
            json={"amount": "500.00", "this_month_only": True},
        )

        assert put.status_code == 200, put.text
        august = by_slug(put.json())["dining"]
        assert august["allocated"] == "500.00"
        assert august["this_month_only"] is True
        september = by_slug((await api_client.get(SEPTEMBER)).json())["dining"]
        assert september["allocated"] == "60.00", "what August carried before"
        assert september["this_month_only"] is False

    async def test_a_new_line_for_this_month_only_is_not_carried(
        self, api_client, db_session
    ):
        _, _, ids = await a_ready_household(api_client, db_session, "+14165579102")

        await api_client.put(
            f"{AUGUST}/lines/{ids['dining']}",
            json={"amount": "300.00", "this_month_only": True},
        )

        assert "dining" not in by_slug((await api_client.get(SEPTEMBER)).json())

    async def test_every_month_is_still_the_default(self, api_client, db_session):
        _, _, ids = await a_ready_household(api_client, db_session, "+14165579103")

        put = await api_client.put(
            f"{AUGUST}/lines/{ids['dining']}", json={"amount": "75.00"}
        )

        assert by_slug(put.json())["dining"]["this_month_only"] is False
        assert (
            by_slug((await api_client.get(SEPTEMBER)).json())["dining"]["allocated"]
            == "75.00"
        )

    async def test_changing_it_back_to_every_month_carries_the_new_amount(
        self, api_client, db_session
    ):
        _, _, ids = await a_ready_household(api_client, db_session, "+14165579104")
        url = f"{AUGUST}/lines/{ids['dining']}"
        await api_client.put(url, json={"amount": "60.00"})
        await api_client.put(url, json={"amount": "500.00", "this_month_only": True})

        await api_client.put(url, json={"amount": "80.00"})

        assert (
            by_slug((await api_client.get(SEPTEMBER)).json())["dining"]["allocated"]
            == "80.00"
        )


ADVICE = {
    "headline": "Your needs are well within your income so far.",
    "tips": [
        "Keep eating out to a weekend treat.",
        "Put $500 into savings.",
        "Move what is left to your emergency buffer first.",
        "A fourth tip is one too many.",
    ],
    "categories": [
        {"category": "dining", "tip": "Plan two meals out a month."},
        {"category": "rent", "tip": "You spent nothing here this month."},
        {"category": "groceries", "tip": "Shop with a list of 10 items."},
    ],
}


async def a_checked_household(api_client, db_session, phone: str):
    household, account, ids = await a_ready_household(api_client, db_session, phone)
    await consent_to_ai(api_client)
    return household, account, ids


class TestTheSpendingCheck:
    async def test_the_figures_are_the_servers(self, api_client, db_session):
        # September so far: groceries $150.01, dining $45, Visa $300, of $5,000.
        await a_checked_household(api_client, db_session, "+14165579201")

        response = await api_client.get(CHECK)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["income"] == "5000.00"
        assert body["needs"] == "450.01", "groceries and the debt payment"
        assert body["wants"] == "45.00"
        assert body["needs_percent"] == 9
        assert body["wants_percent"] == 1
        assert body["needs_aim_percent"] == 50
        assert body["wants_aim_percent"] == 30
        assert body["savings_aim_percent"] == 20
        assert body["unsorted"] == "12.34"
        assert body["headline"] is None and body["tips"] == []
        assert body["runs_left"] == spending_check.MAX_RUNS_A_MONTH

    async def test_the_advice_is_words_about_real_categories(
        self, api_client, db_session, monkeypatch
    ):
        _, _, ids = await a_checked_household(api_client, db_session, "+14165579202")
        model = FakeModel(ADVICE)
        use_model(monkeypatch, model)

        response = await api_client.post(CHECK)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["headline"] == ADVICE["headline"]
        assert body["tips"] == [
            "Keep eating out to a weekend treat.",
            "Move what is left to your emergency buffer first.",
        ], "a tip with a figure is dropped, and only three are read"
        assert [c["slug"] for c in body["categories"]] == [
            "dining"
        ], "not a category with no spending, nor a tip with a figure"
        assert body["categories"][0]["category_id"] == str(ids["dining"])
        assert body["stored"] is False
        assert body["runs_left"] == spending_check.MAX_RUNS_A_MONTH - 1

    async def test_every_figure_and_no_name_is_sent(
        self, api_client, db_session, monkeypatch
    ):
        await a_checked_household(api_client, db_session, "+14165579203")
        model = FakeModel(ADVICE)
        use_model(monkeypatch, model)

        await api_client.post(CHECK)

        _, sent = model.calls[0]
        payload = json.loads(sent)
        assert payload["monthly_income"] == "5000.00"
        assert payload["this_month"]["to_debt"] == "300.00"
        assert {c["category"] for c in payload["by_category"]} >= {
            "groceries",
            "dining",
        }
        assert payload["debts"][0]["minimum"] == "250.00"
        for name in ("Visa", "LOBLAWS", "BISTRO"):
            assert name not in sent

    async def test_the_same_figures_return_the_stored_check(
        self, api_client, db_session, monkeypatch
    ):
        await a_checked_household(api_client, db_session, "+14165579204")
        model = FakeModel(ADVICE)
        use_model(monkeypatch, model)

        await api_client.post(CHECK)
        again = (await api_client.post(CHECK)).json()

        assert len(model.calls) == 1
        assert again["stored"] is True
        assert again["stale"] is False

    async def test_new_spending_makes_it_stale(
        self, api_client, db_session, monkeypatch
    ):
        household, account, ids = await a_checked_household(
            api_client, db_session, "+14165579205"
        )
        use_model(monkeypatch, FakeModel(ADVICE))
        await api_client.post(CHECK)

        db_session.add(
            tx(
                household,
                account,
                minor=8_000,
                month=9,
                day=12,
                category_id=ids["dining"],
                description="SUSHI",
            )
        )
        await db_session.commit()
        body = (await api_client.get(CHECK)).json()

        assert body["stale"] is True
        assert body["wants"] == "125.00"
        assert body["tips"], "the last advice stays until asked again"

    async def test_the_runs_are_capped(self, api_client, db_session, monkeypatch):
        household, account, ids = await a_checked_household(
            api_client, db_session, "+14165579206"
        )
        monkeypatch.setattr(spending_check, "MAX_RUNS_A_MONTH", 1)
        use_model(monkeypatch, FakeModel(ADVICE))
        await api_client.post(CHECK)
        db_session.add(
            tx(
                household,
                account,
                minor=8_000,
                month=9,
                day=12,
                category_id=ids["dining"],
                description="SUSHI",
            )
        )
        await db_session.commit()

        response = await api_client.post(CHECK)

        assert response.status_code == 429
        assert response.json()["detail"]["code"] == "spending_check_limit_reached"

    async def test_without_consent_the_model_is_never_asked(
        self, api_client, db_session, monkeypatch
    ):
        await a_ready_household(api_client, db_session, "+14165579207")
        model = FakeModel(ADVICE)
        use_model(monkeypatch, model)

        response = await api_client.post(CHECK)

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "consent_required"
        assert model.calls == []

    async def test_without_an_income_there_is_nothing_to_check(self, api_client):
        await a_household(api_client, "+14165579208")

        response = await api_client.get(CHECK)

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == endpoint.NO_INCOME

    async def test_one_offs_are_marked_for_the_ai(
        self, api_client, db_session, monkeypatch
    ):
        household, account, ids = await a_checked_household(
            api_client, db_session, "+14165579209"
        )
        tv = tx(
            household,
            account,
            minor=90_000,
            month=9,
            day=11,
            category_id=ids["dining"],
            description="BIG DINNER",
        )
        tv.is_one_off = True
        db_session.add(tv)
        await db_session.commit()
        model = FakeModel(ADVICE)
        use_model(monkeypatch, model)

        await api_client.post(CHECK)

        payload = json.loads(model.calls[0][1])
        dining = next(c for c in payload["by_category"] if c["category"] == "dining")
        assert dining["one_off"] == "900.00"
        stored = (
            await db_session.scalars(select(Transaction).where(Transaction.id == tv.id))
        ).one()
        assert stored.is_one_off is True
