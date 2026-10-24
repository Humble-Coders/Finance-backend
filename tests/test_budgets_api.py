"""`/budgets` — the generated budget, end to end (ticket #56).

The test that matters most here asserts that a budget line and the dashboard
count the same money: if they did not, the two would sit side by side on the
phone disagreeing, and neither would be trusted.

"Today" is pinned to 2026-09-15, so September is the month still running and
the window behind it is June, July and August.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select, text, update

from app.models.categorization import Category
from app.models.enums import ReviewReason, TransactionDirection
from app.models.money import Transaction
from app.models.planning import Budget
from app.services.dashboard import NOT_A_FLOW, countable, month_bounds
from tests.conftest import requires_db
from tests.test_dashboard import a_household, a_system_category_id, setup_wizard, tx

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

TODAY = date(2026, 9, 15)
SEPTEMBER = "/budgets/2026-09"
AUGUST = "/budgets/2026-08"


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr("app.api.budgets._today", lambda: TODAY)


def filler(household, account, count: int, *, month: int = 6) -> list[Transaction]:
    """[count] uncategorised $1 debits: history that never becomes a line."""
    return [
        tx(household, account, minor=100, month=month, day=1, description=f"ROW {i}")
        for i in range(count)
    ]


async def a_ready_household(api_client, db_session, phone: str):
    """A household past the learning threshold, with the scenario below.

    June-August: groceries $400 / $500 / $450, dining once ($90, a one-off),
    transfers and debt payments every month. September so far: groceries
    $150.01, dining $45, transfers $500, debt $300, $12.34 uncategorised — and
    a suspected duplicate and a USD row, which nothing may count. A refund in
    August and one in September: spending is debits, as on the dashboard.
    """
    household, account = await a_household(api_client, phone)
    await setup_wizard(
        api_client,
        income="5000.00",
        debts=[{"name": "Visa", "balance": "2000.00", "minimum_payment": "250.00"}],
    )
    groceries = await a_system_category_id(db_session, "groceries")
    dining = await a_system_category_id(db_session, "dining")
    transfers = await a_system_category_id(db_session, "transfers")
    debt = await a_system_category_id(db_session, "debt_payment")

    def row(minor, month, day, category, description, **kwargs):
        return tx(
            household,
            account,
            minor=minor,
            month=month,
            day=day,
            category_id=category,
            description=description,
            **kwargs,
        )

    db_session.add_all(
        [
            *filler(household, account, 20),
            row(40_000, 6, 10, groceries, "LOBLAWS"),
            row(50_000, 7, 10, groceries, "LOBLAWS"),
            row(45_000, 8, 10, groceries, "LOBLAWS"),
            row(9_000, 8, 12, dining, "BISTRO"),
            *(row(100_000, m, 2, transfers, "TO SAVINGS ACCT") for m in (6, 7, 8)),
            *(row(30_000, m, 3, debt, "VISA PAYMENT") for m in (6, 7, 8)),
            row(5_000, 8, 14, groceries, "LOBLAWS REFUND", credit=True),
            row(12_000, 9, 2, groceries, "LOBLAWS"),
            row(1_000, 9, 9, groceries, "METRO REFUND", credit=True),
            row(3_001, 9, 4, groceries, "METRO"),
            row(4_500, 9, 5, dining, "BISTRO"),
            row(50_000, 9, 2, transfers, "TO SAVINGS ACCT"),
            row(30_000, 9, 3, debt, "VISA PAYMENT"),
            row(1_234, 9, 6, None, "MYSTERY"),
            row(
                99_900,
                9,
                7,
                groceries,
                "LOBLAWS AGAIN",
                needs_review=True,
                reason=ReviewReason.suspected_duplicate,
            ),
            row(77_700, 9, 8, groceries, "WHOLE FOODS NY", currency="USD"),
        ]
    )
    await db_session.commit()
    return household, account, {"groceries": groceries, "dining": dining}


def by_slug(body) -> dict[str, dict]:
    return {line["slug"]: line for line in body["lines"]}


class TestTheGeneratedBudget:
    async def test_lines_come_from_the_median_of_real_spending(
        self, api_client, db_session
    ):
        await a_ready_household(api_client, db_session, "+14165575001")

        response = await api_client.get(SEPTEMBER)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ready"
        assert body["month"] == "2026-09-01"
        assert body["currency"] == "CAD"
        assert body["expected_income"] == "5000.00"
        lines = by_slug(body)
        assert set(lines) == {"groceries"}, "dining was a one-off; transfers move"
        assert lines["groceries"]["suggested"] == "450.00"
        assert lines["groceries"]["allocated"] == "450.00"
        assert lines["groceries"]["is_user_set"] is False
        assert lines["groceries"]["spent"] == "150.01"
        assert body["debt"]["allocated"] == "300.00", "observed $300 beats $250 minimum"
        assert body["debt"]["spent"] == "300.00"
        # Savings is a target the person sets: none yet, only the room for one.
        assert body["savings"] is None
        assert body["savings_room"] == "4250.00"
        assert body["total_allocated"] == "750.00", "groceries and debt; no target"
        assert body["shortfall"] is None
        assert body["uncategorised_spent"] == "12.34"
        assert body["uncategorised_count"] == 1

    async def test_spending_with_no_line_is_reported_and_counted(
        self, api_client, db_session
    ):
        """September's $45 at the bistro has no Dining line (a one-off has a
        median of 0): it is listed, and "spent" counts it, with the $12.34 of
        uncategorised spending. Transfers and savings never are."""
        await a_ready_household(api_client, db_session, "+14165575010")

        body = (await api_client.get(SEPTEMBER)).json()

        assert [(u["slug"], u["spent"]) for u in body["unbudgeted"]] == [
            ("dining", "45.00")
        ]
        assert body["unbudgeted_spent"] == "45.00"
        assert body["total_spent"] == "450.01", "the lines alone, as before"
        assert body["spent_overall"] == "507.35", "450.01 + 45.00 + 12.34"

    async def test_the_month_s_income_and_spending_are_home_s(
        self, api_client, db_session
    ):
        await a_ready_household(api_client, db_session, "+14165575011")

        budget = (await api_client.get(SEPTEMBER)).json()
        home = (await api_client.get("/dashboard", params={"month": "2026-09"})).json()

        assert budget["month_income"] == home["income"]["actual"]
        assert budget["month_spending"] == home["expenses"]["actual"]
        assert budget["month_net"] == home["net"]

    async def test_a_month_with_nothing_has_nothing_unbudgeted(
        self, api_client, db_session
    ):
        await a_ready_household(api_client, db_session, "+14165575012")

        body = (await api_client.get("/budgets/2026-05")).json()

        assert body["unbudgeted"] == []
        assert (
            body["spent_overall"],
            body["month_income"],
            body["month_spending"],
        ) == ("0.00", "0.00", "0.00")

    async def test_spent_is_the_dashboard_s_count(self, api_client, db_session):
        """Each line against `countable`, and the totals against /dashboard."""
        household, _, _ = await a_ready_household(
            api_client, db_session, "+14165575002"
        )
        body = (await api_client.get(SEPTEMBER)).json()
        first, last = month_bounds(date(2026, 9, 1))

        async def debits(*where) -> int:
            return int(
                await db_session.scalar(
                    select(func.coalesce(func.sum(Transaction.amount_minor_units), 0))
                    .outerjoin(Category, Category.id == Transaction.category_id)
                    .where(
                        *countable(household, "CAD"),
                        Transaction.direction == TransactionDirection.debit,
                        Transaction.occurred_on >= first,
                        Transaction.occurred_on <= last,
                        *where,
                    )
                )
            )

        def cents(amount: str) -> int:
            whole, part = amount.split(".")
            return int(whole) * 100 + int(part)

        budgeted = [
            line for line in (*body["lines"], body["debt"], body["savings"]) if line
        ]
        for line in budgeted:
            expected = await debits(Category.slug == line["slug"])
            assert cents(line["spent"]) == expected, line["slug"]

        not_budgeted = await debits(
            Transaction.category_id.is_not(None),
            Category.slug.not_in([line["slug"] for line in budgeted]),
            Category.slug.not_in(NOT_A_FLOW),
        )
        dashboard = (
            await api_client.get("/dashboard", params={"month": "2026-09"})
        ).json()
        # Savings is a budget line but not an expense (#73): money set aside
        # went to another of the household's accounts, not anywhere.
        savings_spent = (body["savings"] or {}).get("spent", "0.00")
        assert cents(body["total_spent"]) - cents(savings_spent) + cents(
            body["uncategorised_spent"]
        ) + not_budgeted == cents(dashboard["expenses"]["actual"])

    async def test_a_suspected_duplicate_and_another_currency_count_nowhere(
        self, api_client, db_session
    ):
        """$999 awaiting review and $777 USD in September: groceries spent
        stays $150.01, and adding them to August would move no suggestion."""
        household, account, ids = await a_ready_household(
            api_client, db_session, "+14165575003"
        )
        db_session.add_all(
            [
                tx(
                    household,
                    account,
                    minor=500_000,
                    month=8,
                    day=20,
                    category_id=ids["groceries"],
                    description="DUPLICATE?",
                    needs_review=True,
                    reason=ReviewReason.suspected_duplicate,
                ),
                tx(
                    household,
                    account,
                    minor=500_000,
                    month=8,
                    day=21,
                    category_id=ids["groceries"],
                    description="IN USD",
                    currency="USD",
                ),
            ]
        )
        await db_session.commit()

        groceries = by_slug((await api_client.get(SEPTEMBER)).json())["groceries"]

        assert groceries["spent"] == "150.01"
        assert groceries["suggested"] == "450.00"


class TestStillLearning:
    async def test_nineteen_transactions_are_not_enough(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165575101")
        db_session.add_all(filler(household, account, 19, month=8))
        await db_session.commit()

        body = (await api_client.get(SEPTEMBER)).json()

        assert body["status"] == "learning"
        assert body["learning"] == {
            "ready": False,
            "complete_months": 1,
            "transactions": 19,
            "needs": {"complete_months": 1, "transactions": 20},
        }
        assert body["lines"] == []

    async def test_a_single_month_still_running_is_not_enough(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165575102")
        db_session.add_all(filler(household, account, 25, month=9))
        await db_session.commit()

        body = (await api_client.get(SEPTEMBER)).json()

        assert body["status"] == "learning"
        assert body["learning"]["complete_months"] == 0
        assert body["learning"]["transactions"] == 25

    async def test_one_complete_month_and_twenty_transactions_is(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165575103")
        db_session.add_all(filler(household, account, 20, month=8))
        await db_session.commit()

        body = (await api_client.get(SEPTEMBER)).json()

        assert body["status"] == "ready"
        assert body["learning"] is None

    async def test_rows_nothing_counts_do_not_count_toward_it(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165575104")
        db_session.add_all(
            [
                *filler(household, account, 19, month=8),
                tx(household, account, minor=100, month=8, currency="USD"),
            ]
        )
        await db_session.commit()

        body = (await api_client.get(SEPTEMBER)).json()

        assert body["learning"]["transactions"] == 19

    async def test_a_learning_household_s_read_writes_nothing(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165575105")
        db_session.add_all(filler(household, account, 19, month=8))
        await db_session.commit()

        await api_client.get(SEPTEMBER)

        budgets = await db_session.scalar(
            select(func.count(Budget.id)).where(Budget.household_id == household)
        )
        assert budgets == 0

    async def test_lines_set_by_hand_show_while_learning(self, api_client, db_session):
        """Manual budgeting is always available: what the user typed is shown,
        and nothing is generated around it — not even savings."""
        household, account = await a_household(api_client, "+14165575106")
        await setup_wizard(api_client, income="5000.00")
        groceries = await a_system_category_id(db_session, "groceries")
        db_session.add_all(
            [
                *filler(household, account, 17, month=8),
                *(
                    tx(
                        household,
                        account,
                        minor=40_000,
                        month=m,
                        category_id=groceries,
                        description="LOBLAWS",
                    )
                    for m in (7, 8)
                ),
            ]
        )
        await db_session.commit()

        put = await api_client.put(
            f"{SEPTEMBER}/lines/{groceries}", json={"amount": "300.00"}
        )
        read = await api_client.get(SEPTEMBER)

        for response in (put, read):
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["status"] == "learning"
            assert body["learning"]["transactions"] == 19
            assert [(line["slug"], line["allocated"]) for line in body["lines"]] == [
                ("groceries", "300.00")
            ]
            assert body["lines"][0]["is_user_set"] is True
            # No savings target until one is set; the room for one shows.
            assert body["savings"] is None
            assert body["savings_room"] == "4700.00"
            assert body["debt"] is None

    async def test_a_line_set_while_learning_is_kept_once_ready(
        self, api_client, db_session
    ):
        """Set on a month that has already ended, while learning. Once the
        threshold is passed that month is generated around it — editing it
        early must not have frozen it empty."""
        household, account = await a_household(api_client, "+14165575107")
        await setup_wizard(api_client, income="5000.00")
        groceries = await a_system_category_id(db_session, "groceries")
        dining = await a_system_category_id(db_session, "dining")
        db_session.add_all(
            [
                *filler(household, account, 17, month=6),
                *(
                    tx(
                        household,
                        account,
                        minor=40_000,
                        month=m,
                        category_id=groceries,
                        description="LOBLAWS",
                    )
                    for m in (6, 7)
                ),
            ]
        )
        await db_session.commit()
        early = await api_client.put(
            f"{AUGUST}/lines/{dining}", json={"amount": "75.00"}
        )
        assert early.json()["status"] == "learning"

        db_session.add(tx(household, account, minor=100, month=8, description="20TH"))
        await db_session.commit()
        body = (await api_client.get(AUGUST)).json()

        assert body["status"] == "ready"
        lines = by_slug(body)
        assert lines["dining"]["allocated"] == "75.00"
        assert lines["dining"]["is_user_set"] is True
        assert lines["groceries"]["allocated"] == "400.00"
        assert body["savings_room"] == "4525.00"

    async def test_dropping_back_below_the_threshold_shows_only_the_user_s_lines(
        self, api_client, db_session
    ):
        """Generated lines stay saved for when the household is ready again,
        but a suggestion from history since deleted is not shown — only the
        user's lines, and what setup alone budgets."""
        household, _, ids = await a_ready_household(
            api_client, db_session, "+14165575108"
        )
        await api_client.put(
            f"{SEPTEMBER}/lines/{ids['dining']}", json={"amount": "60.00"}
        )
        await db_session.execute(
            delete(Transaction).where(
                Transaction.household_id == household,
                Transaction.description.like("ROW %"),
            )
        )
        await db_session.commit()

        body = (await api_client.get(SEPTEMBER)).json()

        assert body["status"] == "learning"
        assert [(line["slug"], line["allocated"]) for line in body["lines"]] == [
            ("dining", "60.00")
        ]
        # Setup's own lines need no history: the Visa's $250 minimum, and
        # savings as the $5,000 income less both lines.
        assert body["debt"]["allocated"] == "250.00"
        assert body["savings_room"] == "4690.00"


class TestTheUserDecides:
    async def test_a_line_set_by_hand_survives_regeneration(
        self, api_client, db_session
    ):
        household, account, ids = await a_ready_household(
            api_client, db_session, "+14165575201"
        )
        put = await api_client.put(
            f"{SEPTEMBER}/lines/{ids['groceries']}", json={"amount": "999.00"}
        )
        assert put.status_code == 200, put.text
        assert by_slug(put.json())["groceries"]["is_user_set"] is True
        assert put.json()["savings_room"] == "3701.00", "rebalanced at once"

        # August's groceries rise to $750: the median moves to $500.
        db_session.add(
            tx(
                household,
                account,
                minor=30_000,
                month=8,
                day=25,
                category_id=ids["groceries"],
                description="COSTCO",
            )
        )
        await db_session.commit()
        groceries = by_slug((await api_client.get(SEPTEMBER)).json())["groceries"]

        assert groceries["allocated"] == "999.00"
        assert groceries["suggested"] == "500.00"
        assert groceries["is_user_set"] is True

    async def test_resetting_restores_the_current_suggestion(
        self, api_client, db_session
    ):
        _, _, ids = await a_ready_household(api_client, db_session, "+14165575202")
        await api_client.put(
            f"{SEPTEMBER}/lines/{ids['groceries']}", json={"amount": "999.00"}
        )

        response = await api_client.delete(
            f"{SEPTEMBER}/lines/{ids['groceries']}/override"
        )

        assert response.status_code == 200, response.text
        groceries = by_slug(response.json())["groceries"]
        assert groceries["allocated"] == "450.00"
        assert groceries["is_user_set"] is False
        assert response.json()["savings_room"] == "4250.00"

    async def test_a_hand_added_line_with_no_history_is_removed_on_reset(
        self, api_client, db_session
    ):
        await a_ready_household(api_client, db_session, "+14165575203")
        education = await a_system_category_id(db_session, "education")

        added = await api_client.put(
            f"{SEPTEMBER}/lines/{education}", json={"amount": "50.00"}
        )
        assert by_slug(added.json())["education"]["suggested"] == "0.00"
        reset = await api_client.delete(f"{SEPTEMBER}/lines/{education}/override")

        assert reset.status_code == 200
        assert "education" not in by_slug(reset.json())
        again = await api_client.delete(f"{SEPTEMBER}/lines/{education}/override")
        assert again.status_code == 404

    async def test_lines_above_income_are_a_shortfall(self, api_client, db_session):
        _, _, ids = await a_ready_household(api_client, db_session, "+14165575204")

        body = (
            await api_client.put(
                f"{SEPTEMBER}/lines/{ids['groceries']}", json={"amount": "6000.00"}
            )
        ).json()

        assert body["savings"] is None
        assert body["shortfall"] == "1300.00"
        assert body["total_allocated"] == "6300.00"

    async def test_amounts_round_trip_exactly(self, api_client, db_session):
        _, _, ids = await a_ready_household(api_client, db_session, "+14165575205")

        body = (
            await api_client.put(
                f"{SEPTEMBER}/lines/{ids['dining']}", json={"amount": "123.45"}
            )
        ).json()

        assert by_slug(body)["dining"]["allocated"] == "123.45"

    @pytest.mark.parametrize(
        "amount", ["abc", "-5.00", "1.001", "", "100000000000000000000"]
    )
    async def test_an_amount_that_is_not_one_is_refused(
        self, api_client, db_session, amount
    ):
        _, _, ids = await a_ready_household(api_client, db_session, "+14165575300")

        response = await api_client.put(
            f"{SEPTEMBER}/lines/{ids['groceries']}", json={"amount": amount}
        )

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_amount"


class TestAMonthThatHasEnded:
    async def test_one_read_before_its_history_fills_in_once_imported(
        self, api_client, db_session
    ):
        """July is read when only August is imported; then April to June
        arrive. July must not stay empty for good."""
        household, account = await a_household(api_client, "+14165575402")
        groceries = await a_system_category_id(db_session, "groceries")
        db_session.add_all(filler(household, account, 20, month=8))
        await db_session.commit()
        before = (await api_client.get("/budgets/2026-07")).json()
        assert before["status"] == "ready"
        assert before["lines"] == []

        db_session.add_all(
            [
                tx(
                    household,
                    account,
                    minor=40_000,
                    month=m,
                    day=10,
                    category_id=groceries,
                    description="LOBLAWS",
                )
                for m in (4, 5, 6)
            ]
        )
        await db_session.commit()
        after = by_slug((await api_client.get("/budgets/2026-07")).json())

        assert after["groceries"]["allocated"] == "400.00"

    async def test_one_read_while_its_history_awaits_review_fills_in_once_filed(
        self, api_client, db_session
    ):
        """June is imported but nothing in it is categorised yet when July is
        read. Rows alone are not history: July fills in once June is filed."""
        household, account = await a_household(api_client, "+14165575404")
        groceries = await a_system_category_id(db_session, "groceries")
        db_session.add_all(filler(household, account, 20, month=6))
        await db_session.commit()
        before = (await api_client.get("/budgets/2026-07")).json()
        assert before["status"] == "ready"
        assert before["lines"] == []

        await db_session.execute(
            update(Transaction)
            .where(Transaction.household_id == household)
            .values(category_id=groceries)
        )
        await db_session.commit()
        after = by_slug((await api_client.get("/budgets/2026-07")).json())

        assert after["groceries"]["allocated"] == "20.00"

    async def test_it_keeps_the_income_it_was_built_against(
        self, api_client, db_session
    ):
        await a_ready_household(api_client, db_session, "+14165575403")
        august = (await api_client.get(AUGUST)).json()
        assert august["expected_income"] == "5000.00"

        await setup_wizard(api_client, income="6000.00")
        again = (await api_client.get(AUGUST)).json()
        september = (await api_client.get(SEPTEMBER)).json()

        assert again["expected_income"] == "5000.00"
        assert again["savings"] == august["savings"]
        assert september["expected_income"] == "6000.00", "a running month moves"

    async def test_it_keeps_its_budget_when_its_inputs_change(
        self, api_client, db_session
    ):
        household, account, ids = await a_ready_household(
            api_client, db_session, "+14165575401"
        )
        # August's window is June and July (nothing before June): $400 / $500.
        first = by_slug((await api_client.get(AUGUST)).json())["groceries"]
        assert first["suggested"] == "450.00"

        db_session.add(
            tx(
                household,
                account,
                minor=200_000,
                month=7,
                day=20,
                category_id=ids["groceries"],
                description="BULK BUY",
            )
        )
        await db_session.commit()
        again = by_slug((await api_client.get(AUGUST)).json())["groceries"]

        assert again["suggested"] == "450.00"
        assert again["allocated"] == "450.00"


class TestWhatCanNeverBeALine:
    @pytest.mark.parametrize("slug", ["income", "transfers"])
    async def test_money_moving_cannot_be_budgeted(self, api_client, db_session, slug):
        await a_ready_household(api_client, db_session, f"+1416557550{len(slug)}")
        category = await a_system_category_id(db_session, slug)

        put = await api_client.put(
            f"{SEPTEMBER}/lines/{category}", json={"amount": "1.00"}
        )
        reset = await api_client.delete(f"{SEPTEMBER}/lines/{category}/override")

        for response in (put, reset):
            assert response.status_code == 422
            assert response.json()["detail"]["code"] == "not_budgetable"


class TestBoundaries:
    async def test_every_route_is_refused_when_the_feature_is_off(
        self, api_client, db_session
    ):
        _, _, ids = await a_ready_household(api_client, db_session, "+14165575601")
        await db_session.execute(
            text(
                "UPDATE feature_availability SET is_enabled = false "
                "WHERE feature_key = 'auto_budget'"
            )
        )
        line = f"{SEPTEMBER}/lines/{ids['groceries']}"

        for response in (
            await api_client.get(SEPTEMBER),
            await api_client.put(line, json={"amount": "1.00"}),
            await api_client.delete(f"{line}/override"),
        ):
            assert response.status_code == 403
            assert response.json()["detail"]["code"] == "feature_unavailable"

    async def test_another_household_s_category_is_not_found(
        self, api_client, db_session
    ):
        await a_household(api_client, "+14165575602")
        own = (await api_client.post("/categories", json={"name": "Pets"})).json()
        await a_ready_household(api_client, db_session, "+14165575603")

        put = await api_client.put(
            f"{SEPTEMBER}/lines/{own['id']}", json={"amount": "1.00"}
        )
        reset = await api_client.delete(f"{SEPTEMBER}/lines/{own['id']}/override")
        unknown = await api_client.put(
            f"{SEPTEMBER}/lines/{uuid.uuid4()}", json={"amount": "1.00"}
        )

        for response in (put, reset, unknown):
            assert response.status_code == 404
            assert response.json()["detail"]["code"] == "not_found"

    async def test_one_household_never_sees_another_s_budget(
        self, api_client, db_session
    ):
        """Both past the threshold, same month: each reads only its own."""
        _, _, ids = await a_ready_household(api_client, db_session, "+14165575604")
        mine = await api_client.put(
            f"{SEPTEMBER}/lines/{ids['groceries']}", json={"amount": "999.00"}
        )
        assert by_slug(mine.json())["groceries"]["allocated"] == "999.00"

        await a_ready_household(api_client, db_session, "+14165575605")
        theirs = (await api_client.get(SEPTEMBER)).json()

        assert theirs["status"] == "ready"
        assert by_slug(theirs)["groceries"]["allocated"] == "450.00"
        assert by_slug(theirs)["groceries"]["is_user_set"] is False

    @pytest.mark.parametrize(
        ("month", "code"),
        [
            ("2026-13", "invalid_month"),
            ("september", "invalid_month"),
            ("2026-10", "month_in_future"),
        ],
    )
    async def test_a_month_that_cannot_be_budgeted_is_refused(
        self, api_client, month, code
    ):
        await a_household(api_client, "+14165575606")

        response = await api_client.get(f"/budgets/{month}")

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == code


async def with_commitments(api_client, *obligations) -> None:
    """The ready household's setup again — the Visa kept — with commitments."""
    await setup_wizard(
        api_client,
        income="5000.00",
        debts=[{"name": "Visa", "balance": "2000.00", "minimum_payment": "250.00"}],
        obligations=list(obligations),
    )


RENT = {"name": "Rent", "monthly_amount": "1800.00", "due_day": 1, "category": "rent"}
LOBLAWS = {"name": "Loblaws", "monthly_amount": "120.00", "category": "groceries"}


class TestCommitmentsHoldTheirLine:
    async def test_a_commitment_never_seen_is_budgeted_from_setup(
        self, api_client, db_session
    ):
        await a_ready_household(api_client, db_session, "+14165577001")
        await with_commitments(api_client, RENT, LOBLAWS)

        body = (await api_client.get(SEPTEMBER)).json()

        lines = by_slug(body)
        assert lines["rent"]["allocated"] == "1800.00", "no rent history at all"
        assert lines["groceries"]["allocated"] == "450.00", "the median beats $120"
        assert body["savings_room"] == "2450.00"

    async def test_a_learning_household_is_budgeted_its_commitments(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165577002")
        db_session.add_all(filler(household, account, 19, month=8))
        await db_session.commit()
        await setup_wizard(api_client, income="5000.00", obligations=[RENT])

        body = (await api_client.get(SEPTEMBER)).json()

        assert body["status"] == "learning"
        assert [(line["slug"], line["allocated"]) for line in body["lines"]] == [
            ("rent", "1800.00")
        ]
        assert body["lines"][0]["is_user_set"] is False
        assert body["savings_room"] == "3200.00"
        count = await db_session.scalar(
            select(func.count())
            .select_from(Budget)
            .where(Budget.household_id == household)
        )
        assert count == 0, "shown from setup, not stored"


class TestTheMonthSoFar:
    async def test_a_running_month_counts_what_is_still_to_come_once(
        self, api_client, db_session
    ):
        await a_ready_household(api_client, db_session, "+14165577101")
        await with_commitments(api_client, RENT, LOBLAWS)

        body = (await api_client.get(SEPTEMBER)).json()

        assert body["month_ended"] is False
        assert [
            (c["name"], c["category"], c["paid_on"], c["paid_amount"])
            for c in body["commitments"]
        ] == [
            ("Rent", "rent", None, None),
            ("Loblaws", "groceries", "2026-09-02", "120.00"),
        ]
        # Rent unseen is due once, and not again as the rent line's remainder.
        assert body["commitments_due"] == "1800.00"
        # Groceries $450 less $150.01 spent; debt fully paid; rent counted above.
        assert body["budget_rest"] == "299.99"
        net = Decimal(body["month_net"])
        expected = Decimal(body["income_expected"])
        assert expected == max(Decimal("5000.00") - Decimal(body["month_income"]), 0)
        assert Decimal(body["projected_left"]) == (
            net + expected - Decimal("1800.00") - Decimal("299.99")
        )

    async def test_an_ended_month_has_nothing_still_to_come(
        self, api_client, db_session
    ):
        await a_ready_household(api_client, db_session, "+14165577102")
        await with_commitments(api_client, RENT)

        body = (await api_client.get(AUGUST)).json()

        assert body["month_ended"] is True
        assert body["commitments"][0]["name"] == "Rent"
        assert body["commitments"][0]["paid_on"] is None
        for key in (
            "income_expected",
            "commitments_due",
            "budget_rest",
            "projected_left",
        ):
            assert body[key] is None

    async def test_a_month_s_spending_by_category_sums_to_its_spending(
        self, api_client, db_session
    ):
        await a_ready_household(api_client, db_session, "+14165577103")

        body = (await api_client.get(AUGUST)).json()

        rows = [(c["slug"], c["spent"]) for c in body["spending_by_category"]]
        assert rows == [
            ("groceries", "450.00"),
            ("debt_payment", "300.00"),
            ("dining", "90.00"),
        ], "transfers are not spending; the refund is not a debit"
        assert sum(Decimal(c["spent"]) for c in body["spending_by_category"]) == (
            Decimal(body["month_spending"])
        )


class TestYourOwnLinesCarryForward:
    async def test_a_line_added_by_hand_is_in_every_month_after(
        self, api_client, db_session
    ):
        _, _, ids = await a_ready_household(api_client, db_session, "+14165577201")
        put = await api_client.put(
            f"{AUGUST}/lines/{ids['dining']}", json={"amount": "60.00"}
        )
        assert put.status_code == 200, put.text

        dining = by_slug((await api_client.get(SEPTEMBER)).json())["dining"]

        assert dining["allocated"] == "60.00", "September started from August's"
        assert dining["is_user_set"] is True

    async def test_a_month_already_made_keeps_its_own(self, api_client, db_session):
        _, _, ids = await a_ready_household(api_client, db_session, "+14165577202")
        await api_client.get(SEPTEMBER)
        await api_client.put(
            f"{AUGUST}/lines/{ids['dining']}", json={"amount": "60.00"}
        )

        lines = by_slug((await api_client.get(SEPTEMBER)).json())

        assert "dining" not in lines, "copied when a month is made, not after"


class TestTheSavingsTarget:
    async def test_none_is_filled_in_until_the_person_sets_one(
        self, api_client, db_session
    ):
        _, _, ids = await a_ready_household(api_client, db_session, "+14165577301")
        body = (await api_client.get(SEPTEMBER)).json()
        assert body["savings"] is None
        assert body["savings_room"] == "4250.00"
        savings = body["savings_category_id"]

        put = await api_client.put(
            f"{SEPTEMBER}/lines/{savings}", json={"amount": "800.00"}
        )

        assert put.status_code == 200, put.text
        target = put.json()["savings"]
        assert target["allocated"] == "800.00"
        assert target["is_user_set"] is True
        assert target["suggested"] == "4250.00", "the room, for the editor to show"
        assert put.json()["savings_due"] == "800.00", "nothing moved to savings yet"
        assert Decimal(put.json()["projected_left"]) == Decimal(
            body["projected_left"]
        ) - Decimal("800.00")

    async def test_removing_the_target_leaves_no_line(self, api_client, db_session):
        await a_ready_household(api_client, db_session, "+14165577302")
        savings = (await api_client.get(SEPTEMBER)).json()["savings_category_id"]
        await api_client.put(f"{SEPTEMBER}/lines/{savings}", json={"amount": "800.00"})

        response = await api_client.delete(f"{SEPTEMBER}/lines/{savings}/override")

        assert response.status_code == 200, response.text
        assert response.json()["savings"] is None

    async def test_left_is_income_less_spending_less_savings(
        self, api_client, db_session
    ):
        household, account, _ = await a_ready_household(
            api_client, db_session, "+14165577303"
        )
        savings = await a_system_category_id(db_session, "savings")
        db_session.add(
            tx(
                household,
                account,
                minor=20_000,
                month=9,
                day=10,
                category_id=savings,
                description="TO SAVINGS",
            )
        )
        await db_session.commit()

        body = (await api_client.get(SEPTEMBER)).json()

        assert body["month_saved"] == "200.00"
        assert Decimal(body["month_left"]) == Decimal(body["month_net"]) - Decimal(
            "200.00"
        )
