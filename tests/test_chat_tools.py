"""The AI coach's data tools (#83), on ten years of data.

Every figure is checked against a sum of the raw transactions made here, by
a different query from the tool's; nothing is checked against the tool's own
arithmetic.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import extract, func, or_, select

from app.models.categorization import Category
from app.models.enums import ReviewReason, TransactionDirection
from app.models.money import Transaction
from app.services import chat_tools, rollups
from app.services.chat_tools import (
    TOOLS,
    data_range,
    handlers_for,
    period_compare,
    spending_summary,
    top_merchants,
    transaction_search,
)
from app.services.dashboard import NOT_A_FLOW
from app.services.llm import ToolError, _schema_problem
from tests.conftest import requires_db
from tests.fixtures.long_history import COMMON_MERCHANT, ten_years
from tests.test_dashboard import a_household, tx

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

TODAY = date(2026, 1, 15)
CAD = "CAD"


async def a_long_history(api_client, db_session, phone: str):
    household, account = await a_household(api_client, phone)
    await ten_years(db_session, household, account)
    return household


def countable_spending(household):
    """Spending as the dashboard counts it, written out longhand."""
    return [
        Transaction.household_id == household,
        Transaction.currency == CAD,
        Transaction.direction == TransactionDirection.debit,
        ~(
            Transaction.needs_review.is_(True)
            & (Transaction.review_reason == ReviewReason.suspected_duplicate)
        ),
        or_(Category.slug.is_(None), Category.slug.not_in(NOT_A_FLOW)),
    ]


async def raw(db_session, *columns, where, group_by=None):
    query = (
        select(*columns)
        .select_from(Transaction)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(*where)
    )
    if group_by is not None:
        query = query.group_by(group_by)
    return (await db_session.execute(query)).all()


def cents(amount: str) -> int:
    return int(Decimal(amount) * 100)


class TestTenYears:
    async def test_spending_by_year_is_ten_rows_equal_to_the_raw_sums(
        self, api_client, db_session
    ):
        household = await a_long_history(api_client, db_session, "+14165578401")

        started = time.perf_counter()
        result = await spending_summary(
            db_session,
            household,
            CAD,
            TODAY,
            from_month="2016-01",
            to_month="2025-12",
            group_by="year",
        )
        elapsed = time.perf_counter() - started

        year = extract("year", Transaction.occurred_on)
        expected = {
            str(int(y)): int(total)
            for y, total in await raw(
                db_session,
                year,
                func.sum(Transaction.amount_minor_units),
                where=countable_spending(household),
                group_by=year,
            )
        }
        assert elapsed < 0.2, f"{elapsed:.3f}s"
        assert result["row_count"] == 10
        assert [r["p"] for r in result["rows"]] == [str(y) for y in range(2016, 2026)]
        assert {r["p"]: cents(r["t"]) for r in result["rows"]} == expected
        assert cents(result["total"]) == sum(expected.values())
        assert all(
            r["one_off"] == "1500.00" for r in result["rows"]
        ), "one laptop a year"

    async def test_by_month_by_category_and_one_total(self, api_client, db_session):
        household = await a_long_history(api_client, db_session, "+14165578402")
        args = dict(from_month="2016-01", to_month="2025-12")

        months = await spending_summary(
            db_session, household, CAD, TODAY, group_by="month", **args
        )
        categories = await spending_summary(
            db_session, household, CAD, TODAY, group_by="category", **args
        )
        one = await spending_summary(
            db_session, household, CAD, TODAY, group_by="none", **args
        )

        assert months["row_count"] == 120
        assert months["rows"][0]["p"] == "2016-01"
        totals = [cents(r["t"]) for r in categories["rows"]]
        assert totals == sorted(totals, reverse=True), "largest first"
        slugs = {r["cat"] for r in categories["rows"]}
        assert "uncategorized" in slugs
        assert not slugs & set(
            NOT_A_FLOW
        ), "moves between own accounts are not spending"
        assert one["total"] == months["total"] == categories["total"]
        assert one["rows"][0]["t"] == one["total"]

    async def test_a_category_filter_and_income(self, api_client, db_session):
        household = await a_long_history(api_client, db_session, "+14165578403")

        groceries = await spending_summary(
            db_session,
            household,
            CAD,
            TODAY,
            from_month="2020-01",
            to_month="2020-12",
            group_by="none",
            categories=["groceries"],
        )
        income = await spending_summary(
            db_session,
            household,
            CAD,
            TODAY,
            from_month="2020-01",
            to_month="2020-12",
            group_by="none",
            direction="in",
        )

        [(expected,)] = await raw(
            db_session,
            func.sum(Transaction.amount_minor_units),
            where=[
                *countable_spending(household),
                Category.slug == "groceries",
                extract("year", Transaction.occurred_on) == 2020,
            ],
        )
        assert cents(groceries["total"]) == expected
        assert income["total"] == "72000.00", "two pay cheques a month"

    async def test_two_years_compared_by_the_server(self, api_client, db_session):
        household = await a_long_history(api_client, db_session, "+14165578404")

        result = await period_compare(
            db_session,
            household,
            CAD,
            TODAY,
            a_from="2016-01",
            a_to="2016-12",
            b_from="2025-01",
            b_to="2025-12",
            group_by="category",
        )

        for row in result["rows"]:
            assert cents(row["diff"]) == cents(row["b"]) - cents(row["a"])
        total = result["total"]
        assert cents(total["diff"]) == cents(total["b"]) - cents(total["a"])
        a, b = cents(total["a"]), cents(total["b"])
        assert total["pct"] == f"{round((b - a) * 100 / a, 1):.1f}"
        rent = next(r for r in result["rows"] if r["cat"] == "rent")
        assert (rent["a"], rent["b"], rent["diff"], rent["pct"]) == (
            "21600.00",
            "21600.00",
            "0.00",
            "0.0",
        )

    async def test_a_common_merchant_over_ten_years_is_a_summary(
        self, api_client, db_session
    ):
        household = await a_long_history(api_client, db_session, "+14165578405")

        result = await transaction_search(
            db_session,
            household,
            CAD,
            TODAY,
            from_date="2016-01-01",
            to_date="2025-12-31",
            merchant="amazon market",
        )

        [(count, total)] = await raw(
            db_session,
            func.count(Transaction.id),
            func.sum(Transaction.amount_minor_units),
            where=[
                *countable_spending(household)[:4],
                Transaction.merchant == COMMON_MERCHANT,
            ],
        )
        assert len(result["rows"]) == 50
        assert result["total_count"] == result["row_count"] == count > 50
        assert cents(result["total_out"]) == total
        assert result["truncated"] is True
        dates = [r["d"] for r in result["rows"]]
        assert dates == sorted(dates, reverse=True), "newest first"
        assert {r["m"] for r in result["rows"]} == {COMMON_MERCHANT}

    async def test_top_merchants_leave_out_transfers_and_other_currencies(
        self, api_client, db_session
    ):
        household = await a_long_history(api_client, db_session, "+14165578406")

        result = await top_merchants(
            db_session,
            household,
            CAD,
            TODAY,
            from_date="2016-01-01",
            to_date="2025-12-31",
        )

        names = [r["m"] for r in result["rows"]]
        assert len(names) == 10 and result["truncated"] is True
        assert result["row_count"] > 10
        assert "TRANSFER TO SAVINGS" not in names
        assert "AMAZON.COM" not in names, "USD"
        by_merchant = await raw(
            db_session,
            Transaction.merchant,
            func.sum(Transaction.amount_minor_units),
            func.count(Transaction.id),
            where=countable_spending(household),
            group_by=Transaction.merchant,
        )
        largest = sorted(by_merchant, key=lambda r: -r[1])[:10]
        assert [(r["m"], cents(r["t"]), r["n"]) for r in result["rows"]] == [
            (m, int(t), n) for m, t, n in largest
        ]
        assert result["row_count"] == len(by_merchant)

    async def test_the_range_of_data_held(self, api_client, db_session):
        household = await a_long_history(api_client, db_session, "+14165578407")

        assert await data_range(db_session, household, CAD) == (
            date(2016, 1, 1),
            date(2025, 12, 1),
        )


class TestWhatAResultMayHold:
    async def test_no_ids_no_floats_money_as_decimal_strings(
        self, api_client, db_session
    ):
        household = await a_long_history(api_client, db_session, "+14165578411")
        handlers = handlers_for(db_session, household, CAD, TODAY)
        results = [
            await handlers["spending_summary"](
                {"from_month": "2016-01", "to_month": "2025-12", "group_by": "category"}
            ),
            await handlers["period_compare"](
                {
                    "a_from": "2016-01",
                    "a_to": "2016-12",
                    "b_from": "2025-01",
                    "b_to": "2025-12",
                }
            ),
            await handlers["transaction_search"](
                {"from_date": "2025-01-01", "to_date": "2025-12-31"}
            ),
            await handlers["top_merchants"](
                {"from_date": "2025-01-01", "to_date": "2025-12-31"}
            ),
        ]

        uuid_text = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-")

        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    assert "id" not in key.lower(), key
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            else:
                assert not isinstance(value, float), value
                if isinstance(value, str):
                    assert not uuid_text.search(value), value

        for result in results:
            walk(result)
            assert "row_count" in result and "truncated" in result
        text = json.dumps(results)
        assert re.search(r'"t": "\d+\.\d{2}"', text)

    async def test_one_households_tools_never_return_anothers(
        self, api_client, db_session
    ):
        household = await a_long_history(api_client, db_session, "+14165578412")
        other, account = await a_household(api_client, "+14165578413")
        db_session.add(
            tx(other, account, minor=999_999, month=8, day=3, description="SECRET SHOP")
        )
        await db_session.flush()
        await rollups.rebuild(db_session, other)
        handlers = handlers_for(db_session, household, CAD, date(2026, 9, 1))

        found = await handlers["transaction_search"](
            {"from_date": "2016-01-01", "to_date": "2026-08-31", "merchant": "secret"}
        )
        tops = await handlers["top_merchants"](
            {"from_date": "2026-08-01", "to_date": "2026-08-31"}
        )
        august = await handlers["spending_summary"](
            {"from_month": "2026-08", "to_month": "2026-08", "group_by": "none"}
        )

        assert found["total_count"] == 0 and found["rows"] == []
        assert tops["rows"] == []
        assert august["total"] == "0.00" and august["row_count"] == 0

    async def test_a_wildcard_in_a_merchant_is_taken_literally(
        self, api_client, db_session
    ):
        household = await a_long_history(api_client, db_session, "+14165578414")

        result = await transaction_search(
            db_session,
            household,
            CAD,
            TODAY,
            from_date="2016-01-01",
            to_date="2025-12-31",
            merchant="%",
        )

        assert result["total_count"] == 0


class TestInput:
    async def test_a_range_with_no_data_is_zeros_not_an_error(
        self, api_client, db_session
    ):
        household, _ = await a_household(api_client, "+14165578421")

        result = await spending_summary(
            db_session,
            household,
            CAD,
            TODAY,
            from_month="2010-01",
            to_month="2010-12",
            group_by="year",
        )

        assert (result["rows"], result["total"], result["row_count"]) == ([], "0.00", 0)

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            ({"from_month": "2026-03", "to_month": "2026-04"}, "future"),
            ({"from_month": "2020-05", "to_month": "2020-01"}, "back to front"),
            ({"from_month": "2020-13", "to_month": "2021-01"}, "YYYY-MM"),
            (
                {"from_month": "2020-01", "to_month": "2020-02", "direction": "up"},
                "out",
            ),
        ],
    )
    async def test_bad_input_goes_back_to_the_model(
        self, api_client, db_session, args, message
    ):
        household, _ = await a_household(api_client, "+14165578422")

        with pytest.raises(ToolError, match=message):
            await spending_summary(
                db_session, household, CAD, TODAY, group_by="year", **args
            )

    async def test_an_end_in_the_future_is_brought_back_to_today(
        self, api_client, db_session
    ):
        household, _ = await a_household(api_client, "+14165578423")

        result = await transaction_search(
            db_session,
            household,
            CAD,
            TODAY,
            from_date="2025-06-01",
            to_date="2026-12-31",
        )

        assert result["to"] == "2026-01-15"

    async def test_limits_are_capped(self, api_client, db_session):
        household = await a_long_history(api_client, db_session, "+14165578424")

        result = await transaction_search(
            db_session,
            household,
            CAD,
            TODAY,
            from_date="2016-01-01",
            to_date="2025-12-31",
            limit=500,
        )

        assert len(result["rows"]) == chat_tools.MAX_SEARCH_ROWS


class TestTheSchemas:
    async def test_every_tool_has_a_spec_and_a_handler(self):
        handlers = handlers_for(None, uuid.uuid4(), CAD, TODAY)
        assert sorted(t.name for t in TOOLS) == sorted(handlers)

    async def test_the_specs_accept_what_the_tools_take(self):
        specs = {t.name: t.parameters for t in TOOLS}
        good = {
            "spending_summary": {
                "from_month": "2016-01",
                "to_month": "2025-12",
                "group_by": "year",
                "categories": ["groceries"],
                "direction": "out",
            },
            "transaction_search": {
                "from_date": "2016-01-01",
                "to_date": "2025-12-31",
                "merchant": "costco",
                "min_amount": "100",
                "limit": 20,
            },
        }
        for name, args in good.items():
            assert _schema_problem(args, specs[name]) is None
        assert _schema_problem(
            {"from_date": "2016-01-01", "to_date": "2025-12-31", "household_id": "x"},
            specs["transaction_search"],
        ), "no argument can name a household"
