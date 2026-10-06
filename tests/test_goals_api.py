"""`/goals` and `/legal/disclaimer`, end to end (backend #65).

"Today" is pinned to 2026-09-15 on every clock the goals read: theirs, and
the budget's for the comparison.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date

import pytest
from sqlalchemy import select, text

from app.auth import AuthenticatedUser, current_user
from app.models.planning import Goal
from tests.conftest import requires_db
from tests.test_budgets_api import a_ready_household, filler
from tests.test_dashboard import a_household

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

TODAY = date(2026, 9, 15)


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr("app.api.goals._today", lambda: TODAY)
    monkeypatch.setattr("app.api.budgets._today", lambda: TODAY)


def goal(**fields) -> dict:
    return {
        "name": "Emergency fund",
        "horizon": "short_term",
        "target": "1000.00",
    } | fields


async def make(api_client, **fields) -> dict:
    response = await api_client.post("/goals", json=goal(**fields))
    assert response.status_code == 201, response.text
    return response.json()


class TestCreatingAndListing:
    async def test_a_goal_comes_back_projected(self, api_client):
        await a_household(api_client, "+14165578001")

        created = await make(
            api_client,
            kind="vacation",
            saved="100.00",
            target_date="2026-11-30",
            monthly_contribution="300.00",
        )

        assert created["kind"] == "vacation"
        assert created["remaining"] == "900.00"
        assert created["required_monthly"] == "300.00", "$900 over Sep, Oct, Nov"
        assert created["projected_completion"] == "2026-11"
        assert created["progress_percent"] == 10
        assert created["status"] == "on_track"
        assert created["achieved_at"] is None
        assert created["priority"] == 0

    async def test_the_list_is_in_order_and_says_which_math_it_is(self, api_client):
        await a_household(api_client, "+14165578002")
        await make(api_client, name="First")
        await make(api_client, name="Second")

        body = (await api_client.get("/goals")).json()

        assert [g["name"] for g in body["goals"]] == ["First", "Second"]
        assert [g["priority"] for g in body["goals"]] == [0, 1]
        assert body["projection_version"] == "v1"
        assert body["assumes_growth"] is False
        assert body["disclaimer_version"] is None, "no long-term goal yet"

    async def test_a_long_term_goal_brings_the_regional_disclaimer(self, api_client):
        await a_household(api_client, "+14165578003")
        await make(
            api_client, name="Retirement", kind="retirement", horizon="long_term"
        )

        body = (await api_client.get("/goals")).json()

        assert body["disclaimer_version"] == "ca-v1"

    async def test_a_learning_household_can_create_and_use_goals(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165578004")
        db_session.add_all(filler(household, account, 3, month=9))
        await db_session.commit()

        created = await make(api_client)
        added = await api_client.post(
            f"/goals/{created['id']}/add", json={"amount": "50.00"}
        )
        body = (await api_client.get("/goals")).json()

        assert added.status_code == 200
        assert body["goals"][0]["saved"] == "50.00"
        assert body["budget"] is None
        assert body["budget_reason"] == "learning"


class TestAddingMoney:
    async def test_reaching_the_target_stamps_the_day(self, api_client):
        await a_household(api_client, "+14165578101")
        created = await make(api_client, saved="900.00")

        body = (
            await api_client.post(
                f"/goals/{created['id']}/add", json={"amount": "100.00"}
            )
        ).json()

        assert body["saved"] == "1000.00"
        assert body["status"] == "achieved"
        assert body["achieved_at"] == "2026-09-15"

    async def test_an_edit_below_the_target_clears_it_and_one_back_restores_it(
        self, api_client
    ):
        await a_household(api_client, "+14165578102")
        created = await make(api_client, saved="1000.00")
        assert created["achieved_at"] == "2026-09-15"

        lowered = (
            await api_client.patch(f"/goals/{created['id']}", json={"saved": "400.00"})
        ).json()
        raised = (
            await api_client.patch(f"/goals/{created['id']}", json={"target": "400.00"})
        ).json()

        assert lowered["achieved_at"] is None
        assert lowered["status"] == "open"
        assert raised["achieved_at"] == "2026-09-15"

    @pytest.mark.parametrize(
        "amount", ["0", "-5.00", "1.001", "abc", "100000000000000000000"]
    )
    async def test_an_amount_that_is_not_one_is_refused(self, api_client, amount):
        await a_household(api_client, "+14165578103")
        created = await make(api_client)

        response = await api_client.post(
            f"/goals/{created['id']}/add", json={"amount": amount}
        )

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_amount"


class TestTwoAddsAtOnce:
    """`saved = saved + :amount`, exercised with two real connections.

    Every other test here runs inside one rolled-back transaction, where two
    requests can never overlap. This one commits real rows to the local test
    database — conftest refuses any other — and removes them afterwards.
    """

    async def test_both_land(self):
        from sqlalchemy.ext.asyncio import AsyncSession

        from app.db import get_engine
        from app.models.enums import GoalHorizon
        from app.models.identity import Household
        from app.services.goals import add_money

        engine = get_engine()
        household = Household()
        async with AsyncSession(engine, expire_on_commit=False) as setup:
            setup.add(household)
            await setup.flush()
            target = Goal(
                household_id=household.id,
                name="Race",
                horizon=GoalHorizon.short_term,
                target_minor_units=100_000,
                saved_minor_units=0,
                currency="CAD",
            )
            setup.add(target)
            await setup.commit()

        try:
            async with AsyncSession(engine) as first, AsyncSession(engine) as second:
                # The first holds the row, uncommitted; the second must wait for
                # it and then add to what it wrote — not to what it read.
                await add_money(first, household.id, target.id, 30_000, TODAY)
                racing = asyncio.create_task(
                    add_money(second, household.id, target.id, 45_000, TODAY)
                )
                await asyncio.sleep(0.3)
                assert not racing.done()
                await first.commit()
                await racing
                await second.commit()

            async with AsyncSession(engine) as check:
                saved = await check.scalar(
                    select(Goal.saved_minor_units).where(Goal.id == target.id)
                )
            assert saved == 75_000
        finally:
            async with AsyncSession(engine) as cleanup:
                await cleanup.execute(
                    text("DELETE FROM household WHERE id = :id"), {"id": household.id}
                )
                await cleanup.commit()


class TestOrdering:
    async def test_the_order_sent_is_the_order_listed(self, api_client):
        await a_household(api_client, "+14165578201")
        a = await make(api_client, name="A")
        b = await make(api_client, name="B")
        c = await make(api_client, name="C")

        response = await api_client.put(
            "/goals/order", json={"ids": [c["id"], a["id"], b["id"]]}
        )

        assert response.status_code == 200, response.text
        listed = (await api_client.get("/goals")).json()["goals"]
        assert [g["name"] for g in listed] == ["C", "A", "B"]

    async def test_a_list_that_is_not_exactly_the_goals_changes_nothing(
        self, api_client
    ):
        await a_household(api_client, "+14165578202")
        a = await make(api_client, name="A")
        b = await make(api_client, name="B")

        for ids in (
            [b["id"]],
            [b["id"], a["id"], str(uuid.uuid4())],
            [a["id"], b["id"], b["id"]],
        ):
            response = await api_client.put("/goals/order", json={"ids": ids})
            assert response.status_code == 422, ids
            assert response.json()["detail"]["code"] == "order_mismatch"

        listed = (await api_client.get("/goals")).json()["goals"]
        assert [g["name"] for g in listed] == ["A", "B"]


class TestTheBudgetComparison:
    async def test_it_is_set_against_4_1_s_savings_line(self, api_client, db_session):
        await a_ready_household(api_client, db_session, "+14165578301")
        await make(api_client, name="Trip", target_date="2026-11-30")  # $333.34 a month
        await make(
            api_client, name="Fund", monthly_contribution="100.00"
        )  # no date: $100

        body = (await api_client.get("/goals")).json()
        savings = (await api_client.get("/budgets/2026-09")).json()["savings"]

        assert body["budget_reason"] is None
        assert body["budget"] == {
            "need": "433.34",
            "set_aside": savings["allocated"],
            "shortfall": None,
        }

    async def test_a_need_past_the_savings_line_is_a_shortfall(
        self, api_client, db_session
    ):
        await a_ready_household(api_client, db_session, "+14165578302")
        await make(
            api_client, target="10000.00", target_date="2026-10-31"
        )  # $5,000 a month

        body = (await api_client.get("/goals")).json()

        assert body["budget"]["set_aside"] == "4250.00"
        assert body["budget"]["shortfall"] == "750.00"

    async def test_no_savings_line_says_so(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165578303")
        db_session.add_all(filler(household, account, 20, month=8))
        await db_session.commit()
        await make(api_client)

        body = (await api_client.get("/goals")).json()

        assert body["budget"] is None
        assert (
            body["budget_reason"] == "no_savings_line"
        ), "no income: nothing left to save"

    async def test_without_the_budget_feature_it_is_unavailable(
        self, api_client, db_session
    ):
        await a_ready_household(api_client, db_session, "+14165578304")
        await make(api_client)
        await db_session.execute(
            text(
                "UPDATE feature_availability SET is_enabled = false "
                "WHERE feature_key = 'auto_budget'"
            )
        )

        body = (await api_client.get("/goals")).json()

        assert body["budget"] is None
        assert body["budget_reason"] == "unavailable"


class TestTheLimit:
    async def test_a_twenty_first_goal_in_progress_is_a_conflict(self, api_client):
        await a_household(api_client, "+14165578401")
        for n in range(20):
            await make(api_client, name=f"Goal {n}")

        refused = await api_client.post("/goals", json=goal(name="One more"))
        achieved = await api_client.post(
            "/goals", json=goal(name="Done", saved="1000.00")
        )

        assert refused.status_code == 409
        assert refused.json()["detail"]["code"] == "goal_limit_reached"
        assert achieved.status_code == 201, "an achieved goal is not in progress"


class TestValidation:
    @pytest.mark.parametrize(
        ("fields", "code"),
        [
            ({"target": "0"}, "invalid_amount"),
            ({"target": "-1.00"}, "invalid_amount"),
            ({"target": "1.001"}, "invalid_amount"),
            ({"saved": "-1.00"}, "invalid_amount"),
            ({"monthly_contribution": "abc"}, "invalid_amount"),
            ({"name": "   "}, "invalid_name"),
            ({"target_date": "2026-09-14"}, "date_in_past"),
        ],
    )
    async def test_a_goal_that_is_not_one_is_refused(self, api_client, fields, code):
        await a_household(api_client, "+14165578501")

        response = await api_client.post("/goals", json=goal(**fields))

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == code

    async def test_an_overdue_goal_s_date_need_not_move_to_rename_it(
        self, api_client, monkeypatch
    ):
        await a_household(api_client, "+14165578502")
        created = await make(api_client, target_date="2026-09-30")
        monkeypatch.setattr("app.api.goals._today", lambda: date(2026, 10, 5))

        renamed = await api_client.patch(
            f"/goals/{created['id']}",
            json={"name": "Renamed", "target_date": "2026-09-30"},
        )
        moved = await api_client.patch(
            f"/goals/{created['id']}", json={"target_date": "2026-10-01"}
        )

        assert renamed.status_code == 200
        assert renamed.json()["status"] == "overdue"
        assert moved.status_code == 422
        assert moved.json()["detail"]["code"] == "date_in_past"

    async def test_a_field_sent_as_null_clears_only_what_may_be_cleared(
        self, api_client
    ):
        await a_household(api_client, "+14165578503")
        created = await make(api_client, kind="car", monthly_contribution="50.00")

        cleared = await api_client.patch(
            f"/goals/{created['id']}", json={"kind": None, "monthly_contribution": None}
        )
        refused = await api_client.patch(
            f"/goals/{created['id']}", json={"target": None}
        )

        assert cleared.json()["kind"] is None
        assert cleared.json()["monthly_contribution"] is None
        assert refused.status_code == 422


class TestBoundaries:
    async def test_every_route_is_refused_when_the_feature_is_off(
        self, api_client, db_session
    ):
        await a_household(api_client, "+14165578601")
        created = await make(api_client)
        await db_session.execute(
            text(
                "UPDATE feature_availability SET is_enabled = false "
                "WHERE feature_key = 'goals'"
            )
        )
        gid = created["id"]

        for response in (
            await api_client.get("/goals"),
            await api_client.post("/goals", json=goal()),
            await api_client.patch(f"/goals/{gid}", json={"name": "x"}),
            await api_client.post(f"/goals/{gid}/add", json={"amount": "1.00"}),
            await api_client.put("/goals/order", json={"ids": [gid]}),
            await api_client.delete(f"/goals/{gid}"),
        ):
            assert response.status_code == 403
            assert response.json()["detail"]["code"] == "feature_unavailable"

    async def test_another_household_s_goal_is_not_found(self, api_client):
        await a_household(api_client, "+14165578602")
        theirs = await make(api_client)
        await a_household(api_client, "+14165578603")
        gid = theirs["id"]

        for response in (
            await api_client.patch(f"/goals/{gid}", json={"name": "Mine now"}),
            await api_client.post(f"/goals/{gid}/add", json={"amount": "1.00"}),
            await api_client.delete(f"/goals/{gid}"),
        ):
            assert response.status_code == 404
            assert response.json()["detail"]["code"] == "not_found"
        assert (await api_client.get("/goals")).json()["goals"] == []

    async def test_a_goal_deleted_is_gone(self, api_client):
        await a_household(api_client, "+14165578604")
        created = await make(api_client)

        assert (await api_client.delete(f"/goals/{created['id']}")).status_code == 204
        assert (await api_client.get("/goals")).json()["goals"] == []


class TestTheDisclaimer:
    async def test_it_is_the_region_s_regional_disclaimer(self, api_client):
        await a_household(api_client, "+14165578701")

        response = await api_client.get("/legal/disclaimer")

        assert response.status_code == 200
        assert response.json()["version"] == "ca-v1"
        assert response.json()["body"]

    async def test_no_region_has_none(self, api_client):
        from app.main import app

        app.dependency_overrides[current_user] = lambda: AuthenticatedUser(
            user_id=str(uuid.uuid4()),
            email="noregion@example.com",
            phone=None,
            claims={"app_metadata": {"provider": "google"}},
        )

        response = await api_client.get("/legal/disclaimer")

        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "no_disclaimer"
