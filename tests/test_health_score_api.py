"""`GET /health-score` — the score, end to end (ticket #57).

"Today" is pinned to 2026-09-15, so August is the last complete month and
the savings window is June to August. The household is `a_ready_household`'s
(see `test_budgets_api.py`) with a $5,000 salary each month, which scores 100
on every component: it saves well over 20 %, spends exactly August's $450
groceries budget, and pays $300 against a $250 minimum.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select, text

from app.models.derived import HealthScoreSnapshot
from app.models.enums import GoalHorizon
from app.models.planning import Budget, Goal
from app.services.health_score import inputs_from_json, result_from_json, score
from tests.conftest import requires_db
from tests.test_budgets_api import a_ready_household, filler
from tests.test_dashboard import a_household, a_system_category_id, setup_wizard, tx

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

SCORE = "/health-score"
DAY_ONE = date(2026, 9, 15)
DAY_TWO = date(2026, 9, 16)
# October is never imported in these scenarios, so on 2 November the last
# complete month has no data.
NOVEMBER = date(2026, 11, 2)


@pytest.fixture
def today(monkeypatch):
    """Pin both clocks the score reads: its own and the budget's."""

    def pin(day: date) -> None:
        monkeypatch.setattr("app.api.health_score._today", lambda: day)
        monkeypatch.setattr("app.api.budgets._today", lambda: day)

    pin(DAY_ONE)
    return pin


async def a_scored_household(api_client, db_session, phone: str):
    household, account, ids = await a_ready_household(api_client, db_session, phone)
    income = await a_system_category_id(db_session, "income")
    db_session.add_all(
        [
            tx(
                household,
                account,
                minor=500_000,
                month=m,
                day=1,
                credit=True,
                category_id=income,
                description="PAYROLL",
            )
            for m in (6, 7, 8)
        ]
    )
    await db_session.commit()
    return household, account, ids


async def snapshots(db_session, household) -> list[HealthScoreSnapshot]:
    return list(
        (
            await db_session.scalars(
                select(HealthScoreSnapshot)
                .where(HealthScoreSnapshot.household_id == household)
                .order_by(HealthScoreSnapshot.scored_on)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


def by_key(body) -> dict[str, dict]:
    return {c["key"]: c for c in body["components"]}


class TestTheScore:
    async def test_a_ready_household_is_scored_and_explained(
        self, api_client, db_session, today
    ):
        await a_scored_household(api_client, db_session, "+14165576001")

        response = await api_client.get(SCORE)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ready"
        assert body["score"] == 100
        assert body["formula_version"] == "v3"
        parts = by_key(body)
        assert {k: (c["score"], c["weight"]) for k, c in parts.items()} == {
            "savings_consistency": (100, "40.00"),
            "spending_vs_budget": (100, "35.00"),
            "debt_payments": (100, "25.00"),
            # v2's fourth part: no goal yet, so it is left out and the other
            # three keep v1's weighting exactly.
            "goal_completion": (None, "0.00"),
        }
        assert parts["spending_vs_budget"]["inputs"] == {
            "lines": [{"slug": "groceries", "allocated": "450.00", "spent": "450.00"}]
        }
        assert parts["debt_payments"]["inputs"] == {
            "debts": 1,
            "debts_with_minimum": 1,
            "required": "250.00",
            "paid": "300.00",
        }
        months = parts["savings_consistency"]["inputs"]["months"]
        assert [m["month"] for m in months] == [
            "2026-06-01",
            "2026-07-01",
            "2026-08-01",
        ]
        assert months[2]["income"] == "5050.00", "the August refund is a credit"
        assert body["history"] == [
            {"scored_on": "2026-09-15", "score": 100, "formula_version": "v3"}
        ]

    async def test_it_reads_the_budget_4_1_serves(self, api_client, db_session, today):
        """Set August's groceries to $300 by hand: $450 spent is 50 % over,
        spending scores 50, and the score is (40×100 + 35×50 + 25×100) / 100
        = 82.5 → 83."""
        _, _, ids = await a_scored_household(api_client, db_session, "+14165576002")
        put = await api_client.put(
            f"/budgets/2026-08/lines/{ids['groceries']}", json={"amount": "300.00"}
        )
        assert put.status_code == 200, put.text

        body = (await api_client.get(SCORE)).json()

        assert by_key(body)["spending_vs_budget"]["score"] == 50
        assert body["score"] == 83

    async def test_nothing_scorable_is_no_score_and_no_snapshot(
        self, api_client, db_session, today
    ):
        """No income, no categorised spending, and a debt with no minimum."""
        household, account = await a_household(api_client, "+14165576003")
        await setup_wizard(api_client, debts=[{"name": "Loan", "balance": "900.00"}])
        db_session.add_all(filler(household, account, 20, month=8))
        await db_session.commit()

        body = (await api_client.get(SCORE)).json()

        assert body["status"] == "ready"
        assert body["score"] is None
        assert not any(c["available"] for c in body["components"])
        assert await snapshots(db_session, household) == []


class TestStillLearning:
    async def test_learning_answers_learning_and_writes_nothing(
        self, api_client, db_session, today
    ):
        household, account = await a_household(api_client, "+14165576101")
        db_session.add_all(filler(household, account, 19, month=8))
        await db_session.commit()

        body = (await api_client.get(SCORE)).json()

        assert body["status"] == "learning"
        assert body["learning"]["transactions"] == 19
        assert body["score"] is None
        assert await snapshots(db_session, household) == []


class TestHistory:
    async def test_a_past_day_is_never_rewritten(self, api_client, db_session, today):
        household, _, ids = await a_scored_household(
            api_client, db_session, "+14165576201"
        )
        assert (await api_client.get(SCORE)).json()["score"] == 100

        today(DAY_TWO)
        await api_client.put(
            f"/budgets/2026-08/lines/{ids['groceries']}", json={"amount": "300.00"}
        )
        body = (await api_client.get(SCORE)).json()

        rows = await snapshots(db_session, household)
        assert [(r.scored_on, r.score) for r in rows] == [(DAY_ONE, 100), (DAY_TWO, 83)]
        assert [h["score"] for h in body["history"]] == [100, 83], "oldest first"

    async def test_a_second_read_the_same_day_updates_that_day_in_place(
        self, api_client, db_session, today
    ):
        """August's budget is settled at $450; $300 more groceries arrive later
        that day: $750 spent is 66.7 % over, so spending scores 33.3 and the
        score is (4000 + 35 × 33.33 + 2500) / 100 = 76.67 → 77."""
        household, account, ids = await a_scored_household(
            api_client, db_session, "+14165576202"
        )
        assert (await api_client.get(SCORE)).json()["score"] == 100
        db_session.add(
            tx(
                household,
                account,
                minor=30_000,
                month=8,
                day=28,
                category_id=ids["groceries"],
                description="COSTCO",
            )
        )
        await db_session.commit()

        assert (await api_client.get(SCORE)).json()["score"] == 77
        rows = await snapshots(db_session, household)
        assert [(r.scored_on, r.score) for r in rows] == [(DAY_ONE, 77)]

    async def test_the_stored_breakdown_reproduces_the_stored_score(
        self, api_client, db_session, today
    ):
        household, _, ids = await a_scored_household(
            api_client, db_session, "+14165576203"
        )
        await api_client.put(
            f"/budgets/2026-08/lines/{ids['groceries']}", json={"amount": "300.00"}
        )
        await api_client.get(SCORE)

        (row,) = await snapshots(db_session, household)

        assert row.formula_version == "v3"
        assert row.components["formula_version"] == "v3"
        assert score(inputs_from_json(row.components)).score == row.score == 83

    async def test_history_is_the_last_twelve(self, api_client, db_session, today):
        household, _, _ = await a_scored_household(
            api_client, db_session, "+14165576204"
        )
        for day in range(1, 15):
            today(date(2026, 9, day))
            await api_client.get(SCORE)

        today(DAY_ONE)
        body = (await api_client.get(SCORE)).json()

        assert len(body["history"]) == 12
        assert body["history"][0]["scored_on"] == "2026-09-04"
        assert body["history"][-1]["scored_on"] == "2026-09-15"
        assert (
            await db_session.scalar(
                select(func.count(HealthScoreSnapshot.id)).where(
                    HealthScoreSnapshot.household_id == household
                )
            )
            == 15
        )


class TestBoundaries:
    async def test_refused_when_the_feature_is_off(self, api_client, db_session, today):
        await a_scored_household(api_client, db_session, "+14165576301")
        await db_session.execute(
            text(
                "UPDATE feature_availability SET is_enabled = false "
                "WHERE feature_key = 'health_score'"
            )
        )

        response = await api_client.get(SCORE)

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "feature_unavailable"

    async def test_one_household_never_sees_another_s_history(
        self, api_client, db_session, today
    ):
        await a_scored_household(api_client, db_session, "+14165576302")
        await api_client.get(SCORE)
        today(DAY_TWO)
        await api_client.get(SCORE)

        theirs, _, _ = await a_scored_household(api_client, db_session, "+14165576303")
        body = (await api_client.get(SCORE)).json()

        assert [h["scored_on"] for h in body["history"]] == ["2026-09-16"]
        assert len(await snapshots(db_session, theirs)) == 1


class TestAMonthNotYetImported:
    """Last month has no data yet: hold the latest score and say why."""

    async def test_the_latest_score_is_held_with_a_line_saying_why(
        self, api_client, db_session, today
    ):
        household, _, _ = await a_scored_household(
            api_client, db_session, "+14165576401"
        )
        assert (await api_client.get(SCORE)).json()["score"] == 100

        today(NOVEMBER)
        body = (await api_client.get(SCORE)).json()

        assert body["status"] == "ready"
        assert body["score"] == 100
        assert body["held_from"] == "2026-09-15"
        assert body["notice"] == {
            "code": "last_month_missing",
            "month": "2026-10-01",
            "message": (
                "No data for October 2026 is available yet, "
                "so this is your score from 15 Sep 2026."
            ),
        }
        assert by_key(body)["spending_vs_budget"]["inputs"]["lines"][0] == {
            "slug": "groceries",
            "allocated": "450.00",
            "spent": "450.00",
        }, "the held score's own breakdown, not October's"
        assert [r.scored_on for r in await snapshots(db_session, household)] == [
            DAY_ONE
        ], "nothing is written for a month that is not in yet"
        october = await db_session.scalar(
            select(func.count(Budget.id)).where(
                Budget.household_id == household,
                Budget.period_start == date(2026, 10, 1),
            )
        )
        assert october == 0, "and October's budget is not settled from nothing"

    async def test_a_snapshot_from_an_older_formula_is_held_as_it_was(
        self, api_client, db_session, today
    ):
        """Held means read back, not recomputed under today's formula."""
        household, _, _ = await a_scored_household(
            api_client, db_session, "+14165576404"
        )
        await api_client.get(SCORE)
        await db_session.execute(
            text(
                "UPDATE health_score_snapshot SET score = 41, formula_version = 'v0' "
                "WHERE household_id = :household"
            ),
            {"household": household},
        )
        await db_session.commit()

        today(NOVEMBER)
        body = (await api_client.get(SCORE)).json()

        assert (body["score"], body["formula_version"]) == (41, "v0")

    async def test_with_no_score_to_hold_it_says_so(
        self, api_client, db_session, today
    ):
        household, _, _ = await a_scored_household(
            api_client, db_session, "+14165576402"
        )

        today(NOVEMBER)
        body = (await api_client.get(SCORE)).json()

        assert body["status"] == "ready"
        assert body["score"] is None
        assert body["components"] == []
        assert body["held_from"] is None
        assert body["notice"]["message"] == (
            "No data for October 2026 is available yet. "
            "Your score will appear once it is imported."
        )
        assert await snapshots(db_session, household) == []

    async def test_it_is_scored_again_once_the_month_arrives(
        self, api_client, db_session, today
    ):
        household, account, _ = await a_scored_household(
            api_client, db_session, "+14165576403"
        )
        await api_client.get(SCORE)
        today(NOVEMBER)
        assert (await api_client.get(SCORE)).json()["notice"] is not None

        income = await a_system_category_id(db_session, "income")
        db_session.add(
            tx(
                household,
                account,
                minor=500_000,
                month=10,
                day=1,
                credit=True,
                category_id=income,
                description="PAYROLL",
            )
        )
        await db_session.commit()
        body = (await api_client.get(SCORE)).json()

        assert body["notice"] is None
        assert body["held_from"] is None
        assert [r.scored_on for r in await snapshots(db_session, household)] == [
            DAY_ONE,
            NOVEMBER,
        ]


class TestNothingScorableAnyMore:
    async def test_today_s_snapshot_goes_rather_than_outlive_today_s_score(
        self, api_client, db_session, today
    ):
        """Scored earlier today on debt alone (no debts: 100). Then a debt with
        no minimum is added: nothing is scorable, and today's row must not go
        on saying 100."""
        household, account = await a_household(api_client, "+14165576501")
        db_session.add_all(filler(household, account, 20, month=8))
        await db_session.commit()
        assert (await api_client.get(SCORE)).json()["score"] == 100
        assert len(await snapshots(db_session, household)) == 1

        await setup_wizard(api_client, debts=[{"name": "Loan", "balance": "900.00"}])
        body = (await api_client.get(SCORE)).json()

        assert body["score"] is None
        assert body["history"] == []
        assert await snapshots(db_session, household) == []


class TestFormulaV2:
    """Goal completion joins the score as formula v2 (backend #66)."""

    async def a_goal(self, db_session, household, *, saved: int, created: datetime):
        db_session.add(
            Goal(
                household_id=household,
                name="House",
                horizon=GoalHorizon.long_term,
                target_minor_units=120_000,
                saved_minor_units=saved,
                currency="CAD",
                target_date=date(2026, 12, 31),
                created_at=created,
            )
        )
        await db_session.commit()

    async def test_goals_reach_the_score(self, api_client, db_session, today):
        """Created June, due December: 7 months, 3 gone by the end of August,
        so it should hold 3/7 of $1,200. Half of that is 50 for goals, and the
        score is (34 + 29.75 + 21.25) + 15 × 0.5 = 92.5 → 93."""
        household, _, _ = await a_scored_household(
            api_client, db_session, "+14165576701"
        )
        await self.a_goal(
            db_session,
            household,
            saved=25_715,  # a cent over half of 51,428.57
            created=datetime(2026, 6, 1, tzinfo=UTC),
        )

        body = (await api_client.get(SCORE)).json()

        parts = by_key(body)
        assert parts["goal_completion"]["score"] == 50
        assert parts["goal_completion"]["weight"] == "15.00"
        assert parts["savings_consistency"]["weight"] == "34.00"
        assert body["score"] == 93
        assert body["formula_version"] == "v3"

    async def test_a_goal_made_last_month_does_not_count_yet(
        self, api_client, db_session, today
    ):
        household, _, _ = await a_scored_household(
            api_client, db_session, "+14165576702"
        )
        await self.a_goal(
            db_session, household, saved=0, created=datetime(2026, 8, 20, tzinfo=UTC)
        )

        body = (await api_client.get(SCORE)).json()

        assert by_key(body)["goal_completion"]["available"] is False
        assert body["score"] == 100, "the other three, renormalised as v1 weighted them"

    async def test_yesterday_s_v1_snapshot_is_left_as_it_was(
        self, api_client, db_session, today
    ):
        household, _, _ = await a_scored_household(
            api_client, db_session, "+14165576703"
        )
        v1 = {
            "formula_version": "v1",
            "inputs": {
                "months": [
                    {"month": "2026-08-01", "income": 100_000, "expenses": 90_000}
                ],
                "budget_lines": [
                    {"slug": "groceries", "allocated": 40_000, "spent": 50_000}
                ],
                "debt": {"debts": 0, "debts_with_minimum": 0, "required": 0, "paid": 0},
            },
            "components": [
                {
                    "key": "savings_consistency",
                    "score": "50",
                    "weight": "40",
                    "available": True,
                },
                {
                    "key": "spending_vs_budget",
                    "score": "75",
                    "weight": "35",
                    "available": True,
                },
                {
                    "key": "debt_payments",
                    "score": "100",
                    "weight": "25",
                    "available": True,
                },
            ],
        }
        db_session.add(
            HealthScoreSnapshot(
                household_id=household,
                scored_on=date(2026, 9, 14),
                score=71,
                formula_version="v1",
                components=v1,
            )
        )
        await db_session.commit()

        await api_client.get(SCORE)

        yesterday, today_row = await snapshots(db_session, household)
        assert (yesterday.scored_on, yesterday.score, yesterday.formula_version) == (
            date(2026, 9, 14),
            71,
            "v1",
        )
        assert yesterday.components == v1
        assert (
            result_from_json(
                yesterday.components, yesterday.score, yesterday.formula_version
            ).score
            == 71
        )
        assert score(inputs_from_json(yesterday.components)).score == 71
        assert today_row.formula_version == "v3"


class TestNoChangeAcrossFormulas:
    """Home's "+4 since last month" compares like with like (backend #66)."""

    @pytest.fixture(autouse=True)
    def dashboard_today(self, monkeypatch):
        """The dashboard's own clock, which the `today` fixture leaves alone."""
        monkeypatch.setattr("app.api.dashboard._today", lambda: DAY_ONE)

    async def last_month(self, db_session, household, version: str, value: int = 60):
        db_session.add(
            HealthScoreSnapshot(
                household_id=household,
                scored_on=date(2026, 8, 20),
                score=value,
                formula_version=version,
                components=None,
            )
        )
        await db_session.commit()

    async def test_a_v1_score_last_month_gives_no_change(
        self, api_client, db_session, today
    ):
        household, _, _ = await a_scored_household(
            api_client, db_session, "+14165576801"
        )
        await self.last_month(db_session, household, "v1")

        score_out = (await api_client.get("/dashboard")).json()["health_score"]

        assert score_out["formula_version"] == "v3"
        assert score_out["previous_score"] is None

    async def test_a_v2_score_last_month_gives_no_change(
        self, api_client, db_session, today
    ):
        """v3 (#73) counts income and expenses without own-account moves, so a
        v2 score is the old counting, not last month's household."""
        household, _, _ = await a_scored_household(
            api_client, db_session, "+14165576803"
        )
        await self.last_month(db_session, household, "v2")

        score_out = (await api_client.get("/dashboard")).json()["health_score"]

        assert score_out["formula_version"] == "v3"
        assert score_out["previous_score"] is None

    async def test_a_v3_score_last_month_gives_the_change(
        self, api_client, db_session, today
    ):
        household, _, _ = await a_scored_household(
            api_client, db_session, "+14165576802"
        )
        await self.last_month(db_session, household, "v3")

        score_out = (await api_client.get("/dashboard")).json()["health_score"]

        assert score_out["previous_score"] == 60
