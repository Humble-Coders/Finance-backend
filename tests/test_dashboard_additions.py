"""`GET /dashboard` — the budget, the score and freshness (ticket #58).

"Today" is pinned to 2026-09-15 on every clock the dashboard reads. The
household is `a_scored_household`'s (`test_health_score_api.py`): June to
August with a salary, and September so far — groceries $150.01, dining $45,
transfers $500, a debt payment of $300, $12.34 uncategorised, plus a refund,
a suspected duplicate and a USD row that nothing may count.

The existing `/dashboard` tests (`test_dashboard.py`) run unmodified: that is
the check that an older app still decodes the response.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import event, func, select, text

from app.models.derived import HealthScoreSnapshot
from app.models.enums import SourceKind, StatementImportStatus
from app.models.money import StatementImport
from app.models.planning import Budget
from tests.conftest import requires_db
from tests.test_budgets_api import filler
from tests.test_dashboard import a_household, a_system_category_id, tx
from tests.test_health_score_api import a_scored_household

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

DASHBOARD = "/dashboard"
SEPTEMBER = {"month": "2026-09"}
AUGUST = {"month": "2026-08"}
TODAY = date(2026, 9, 15)


@pytest.fixture
def today(monkeypatch):
    """Pin every clock the dashboard, the budget and the score read."""

    def pin(day: date) -> None:
        for module in ("dashboard", "budgets", "health_score"):
            monkeypatch.setattr(f"app.api.{module}._today", lambda d=day: d)

    pin(TODAY)
    return pin


def cents(amount: str) -> int:
    whole, part = amount.split(".")
    return int(whole) * 100 + int(part)


async def snapshot_count(db_session, household) -> int:
    return await db_session.scalar(
        select(func.count(HealthScoreSnapshot.id)).where(
            HealthScoreSnapshot.household_id == household
        )
    )


class TestSpendByCategory:
    async def test_it_sums_to_expenses_exactly(self, api_client, db_session, today):
        """Several categories, an uncategorised row, a suspected duplicate and
        a USD row: the entries still add up to `expenses.actual` to the cent."""
        await a_scored_household(api_client, db_session, "+14165577001")

        body = (await api_client.get(DASHBOARD, params=SEPTEMBER)).json()

        entries = body["spend_by_category"]
        assert sum(cents(e["spent"]) for e in entries) == cents(
            body["expenses"]["actual"]
        )
        assert [(e["slug"], e["spent"]) for e in entries] == [
            ("transfers", "500.00"),
            ("debt_payment", "300.00"),
            ("groceries", "150.01"),
            ("dining", "45.00"),
            (None, "12.34"),
        ], "largest first; the duplicate and the USD row are nowhere"
        uncategorised = entries[-1]
        assert uncategorised["category_id"] is None
        assert uncategorised["name"] is None


class TestTheBudget:
    async def test_every_line_is_the_one_budgets_serves(
        self, api_client, db_session, today
    ):
        _, _, ids = await a_scored_household(api_client, db_session, "+14165577101")
        await api_client.put(
            f"/budgets/2026-09/lines/{ids['groceries']}", json={"amount": "100.00"}
        )

        dashboard = (await api_client.get(DASHBOARD, params=SEPTEMBER)).json()
        served = (await api_client.get("/budgets/2026-09")).json()

        budget = dashboard["budget"]
        assert budget["status"] == "ready"
        assert [
            (line["category_id"], line["allocated"], line["spent"])
            for line in budget["lines"]
        ] == [
            (line["category_id"], line["allocated"], line["spent"])
            for line in served["lines"]
        ], "same lines, same order"
        assert budget["lines"][0]["over"] == "50.01", "$150.01 against $100"
        for key in ("savings", "debt"):
            assert budget[key]["allocated"] == served[key]["allocated"]
        assert budget["total_allocated"] == served["total_allocated"]
        assert budget["total_spent"] == served["total_spent"]
        assert budget["debt"]["over"] == "0.00"

    async def test_a_month_not_yet_begun_has_no_budget_and_creates_none(
        self, api_client, db_session, today
    ):
        household, _, _ = await a_scored_household(
            api_client, db_session, "+14165577102"
        )

        body = (await api_client.get(DASHBOARD, params={"month": "2026-10"})).json()

        assert body["budget"] is None
        assert body["health_score"] is None
        assert (
            await db_session.scalar(
                select(func.count(Budget.id)).where(
                    Budget.household_id == household,
                    Budget.period_start == date(2026, 10, 1),
                )
            )
            == 0
        )


class TestTheScore:
    async def test_the_current_month_carries_today_s_score(
        self, api_client, db_session, today
    ):
        """Equal to GET /health-score, with last month's snapshot beside it."""
        await a_scored_household(api_client, db_session, "+14165577201")
        today(date(2026, 8, 20))
        august = (await api_client.get("/health-score")).json()
        today(TODAY)

        dashboard = (await api_client.get(DASHBOARD, params=SEPTEMBER)).json()
        served = (await api_client.get("/health-score")).json()

        score = dashboard["health_score"]
        assert score["status"] == "ready"
        assert (score["score"], score["formula_version"]) == (
            served["score"],
            served["formula_version"],
        )
        assert score["scored_on"] == "2026-09-15"
        assert score["previous_score"] == august["score"]
        assert score["notice"] is None

    async def test_a_past_month_shows_its_last_snapshot_and_computes_nothing(
        self, api_client, db_session, today
    ):
        household, _, _ = await a_scored_household(
            api_client, db_session, "+14165577202"
        )
        today(date(2026, 8, 20))
        august = (await api_client.get("/health-score")).json()
        today(date(2026, 9, 16))
        before = await snapshot_count(db_session, household)

        past = (await api_client.get(DASHBOARD, params=AUGUST)).json()
        older = (await api_client.get(DASHBOARD, params={"month": "2026-07"})).json()

        assert past["health_score"]["score"] == august["score"]
        assert past["health_score"]["scored_on"] == "2026-08-20"
        assert older["health_score"]["score"] is None, "nothing on or before Jul 31"
        assert (
            await snapshot_count(db_session, household) == before
        ), "reading a past month writes no snapshot, not even today's"

    async def test_a_held_score_is_held_here_too(self, api_client, db_session, today):
        """October not imported: the same held score and notice as /health-score."""
        await a_scored_household(api_client, db_session, "+14165577203")
        await api_client.get("/health-score")
        today(date(2026, 11, 2))

        dashboard = (await api_client.get(DASHBOARD)).json()
        served = (await api_client.get("/health-score")).json()

        score = dashboard["health_score"]
        assert score["score"] == served["score"] == 100
        assert score["scored_on"] == served["held_from"] == "2026-09-15"
        assert score["notice"] == served["notice"]

    async def test_a_held_score_is_compared_with_the_month_before_it(
        self, api_client, db_session, today
    ):
        """Scored 100 on 15 Sep and 87 on 15 Oct (September's savings are a
        refund and no salary). On 2 Nov, October is not in, so 15 Oct's 87 is
        held — and the change is from September's 100, not from itself."""
        await a_scored_household(api_client, db_session, "+14165577204")
        assert (await api_client.get("/health-score")).json()["score"] == 100
        today(date(2026, 10, 15))
        assert (await api_client.get("/health-score")).json()["score"] == 87
        today(date(2026, 11, 2))

        score = (await api_client.get(DASHBOARD)).json()["health_score"]

        assert score["score"] == 87
        assert score["scored_on"] == "2026-10-15"
        assert score["previous_score"] == 100


class TestStillLearning:
    async def test_one_learning_state_for_budget_and_score(
        self, api_client, db_session, today
    ):
        household, account = await a_household(api_client, "+14165577301")
        db_session.add_all(filler(household, account, 19, month=8))
        await db_session.commit()

        body = (await api_client.get(DASHBOARD, params=SEPTEMBER)).json()

        assert body["learning"]["ready"] is False
        assert body["learning"]["transactions"] == 19
        assert body["budget"]["status"] == "learning"
        assert body["budget"]["lines"] == []
        assert body["health_score"] == {
            "status": "learning",
            "learning": body["learning"],
            "score": None,
            "formula_version": None,
            "scored_on": None,
            "previous_score": None,
            "notice": None,
        }
        assert await snapshot_count(db_session, household) == 0


class TestAsOf:
    async def test_nothing_yet_is_null(self, api_client, today):
        await a_household(api_client, "+14165577401")

        body = (await api_client.get(DASHBOARD, params=SEPTEMBER)).json()

        assert body["as_of"] == {
            "latest_transaction_on": None,
            "last_import_at": None,
        }

    async def test_the_newest_transaction_and_import_with_rows(
        self, api_client, db_session, today
    ):
        household, account, ids = await a_scored_household(
            api_client, db_session, "+14165577402"
        )
        with_rows = StatementImport(
            household_id=household,
            source_kind=SourceKind.pdf_text,
            status=StatementImportStatus.awaiting_review,
            created_at=datetime(2026, 9, 10, 9, 30, tzinfo=UTC),
        )
        empty = StatementImport(
            household_id=household,
            source_kind=SourceKind.pdf_text,
            status=StatementImportStatus.awaiting_review,
            created_at=datetime(2026, 9, 12, 8, 0, tzinfo=UTC),
        )
        db_session.add_all([with_rows, empty])
        await db_session.flush()
        row = tx(
            household,
            account,
            minor=2_000,
            month=9,
            day=11,
            category_id=ids["dining"],
            description="CAFE",
        )
        row.statement_import_id = with_rows.id
        db_session.add(row)
        await db_session.commit()

        body = (await api_client.get(DASHBOARD, params=AUGUST)).json()

        assert (
            body["as_of"]["latest_transaction_on"] == "2026-09-11"
        ), "household-wide, whichever month is shown"
        assert body["as_of"]["last_import_at"].startswith(
            "2026-09-10T09:30"
        ), "the later import saved no rows"


class TestFeaturesOff:
    @pytest.mark.parametrize(
        ("feature", "field"),
        [("auto_budget", "budget"), ("health_score", "health_score")],
    )
    async def test_the_field_is_null_and_nothing_else_moves(
        self, api_client, db_session, today, feature, field
    ):
        await a_scored_household(api_client, db_session, "+14165577501")
        on = (await api_client.get(DASHBOARD, params=AUGUST)).json()
        await db_session.execute(
            text(
                "UPDATE feature_availability SET is_enabled = false "
                "WHERE feature_key = :key"
            ),
            {"key": feature},
        )

        off = (await api_client.get(DASHBOARD, params=AUGUST)).json()

        assert on[field] is not None
        assert off[field] is None
        assert {k: v for k, v in off.items() if k != field} == {
            k: v for k, v in on.items() if k != field
        }


class TestNoQueryPerRow:
    async def test_the_statement_count_does_not_grow_with_categories(
        self, api_client, db_session, today
    ):
        """Two households, one with two budgeted categories and one with
        eight: a dashboard read runs the same number of statements for both,
        on the first read of the month and on every read after."""

        async def statements_for_a_read() -> int:
            connection = (await db_session.connection()).sync_connection
            seen: list[str] = []

            def count(*_args) -> None:
                seen.append("x")

            event.listen(connection, "before_cursor_execute", count)
            try:
                response = await api_client.get(DASHBOARD, params=SEPTEMBER)
            finally:
                event.remove(connection, "before_cursor_execute", count)
            assert response.status_code == 200, response.text
            return len(seen)

        async def a_household_with(phone: str, slugs: list[str]) -> tuple[int, int]:
            household, account, _ = await a_scored_household(
                api_client, db_session, phone
            )
            for slug in slugs:
                category = await a_system_category_id(db_session, slug)
                db_session.add_all(
                    [
                        tx(
                            household,
                            account,
                            minor=10_000,
                            month=m,
                            day=20,
                            category_id=category,
                            description=f"{slug} {m}",
                        )
                        for m in (6, 7, 8, 9)
                    ]
                )
            await db_session.commit()
            # The first read generates the budgets (their lines go in as one
            # batched insert); the second is the steady state. Neither grows.
            return (await statements_for_a_read(), await statements_for_a_read())

        few = await a_household_with("+14165577601", ["shopping"])
        many = await a_household_with(
            "+14165577602",
            [
                "shopping",
                "utilities",
                "transport",
                "subscriptions",
                "healthcare",
                "entertainment",
                "education",
            ],
        )

        assert few == many


class TestASectionThatFails:
    """The budget and the score are additions: a fault in either must not take
    Home down, nor leave half of what it wrote behind."""

    async def test_a_failing_budget_is_left_out(
        self, api_client, db_session, today, monkeypatch
    ):
        await a_scored_household(api_client, db_session, "+14165577701")
        whole = (await api_client.get(DASHBOARD, params=SEPTEMBER)).json()

        async def broken(*_args, **_kwargs):
            raise RuntimeError("budget unavailable")

        monkeypatch.setattr("app.services.budget.budget_for", broken)
        response = await api_client.get(DASHBOARD, params=SEPTEMBER)

        assert response.status_code == 200
        body = response.json()
        assert body["budget"] is None
        assert body["net"] == whole["net"]
        assert body["spend_by_category"] == whole["spend_by_category"]
        assert body["health_score"]["score"] == whole["health_score"]["score"]

    async def test_a_failing_score_is_left_out_and_writes_nothing(
        self, api_client, db_session, today, monkeypatch
    ):
        household, _, _ = await a_scored_household(
            api_client, db_session, "+14165577702"
        )
        from app.services import health_score

        real = health_score.current_score

        async def fails_after_writing(*args, **kwargs):
            await real(*args, **kwargs)
            raise RuntimeError("score unavailable")

        monkeypatch.setattr(
            "app.services.health_score.current_score", fails_after_writing
        )
        response = await api_client.get(DASHBOARD, params=SEPTEMBER)

        assert response.status_code == 200
        assert response.json()["health_score"] is None
        assert response.json()["budget"] is not None
        assert (
            await snapshot_count(db_session, household) == 0
        ), "the snapshot it wrote before failing is rolled back with it"
