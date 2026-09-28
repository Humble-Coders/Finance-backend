"""The review queue: what it shows, whose rows it shows, and how it pages.

The paging tests are the load-bearing ones. A queue exists so that every
uncertain row is seen by a person exactly once, and the two ways paging breaks
that promise — skipping a row, or showing one twice — are both invisible from
inside a single page.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.auth import AuthenticatedUser, current_user
from app.models.enums import ReviewReason, TransactionDirection, TransactionSource
from app.models.money import Transaction
from tests.conftest import requires_db

pytestmark = [pytest.mark.asyncio(loop_scope="session"), requires_db]


def authenticate_as(*, phone: str) -> None:
    from app.main import app

    app.dependency_overrides[current_user] = lambda: AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        email=None,
        phone=phone,
        claims={"app_metadata": {"provider": "phone"}},
    )


async def a_household(api_client, phone: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Onboard a caller, give them one account; return (household, account)."""
    authenticate_as(phone=phone)
    me = (await api_client.get("/me")).json()
    version = (await api_client.get("/legal/terms")).json()["version"]
    await api_client.post("/me/consent", json={"version": version})
    account = await api_client.post(
        "/accounts", json={"name": "RBC Chequing", "kind": "chequing"}
    )
    assert account.status_code == 201, account.text
    return uuid.UUID(me["household"]["id"]), uuid.UUID(account.json()["id"])


def flagged(
    household_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    day: int,
    minor: int = 1099,
    needs_review: bool = True,
    reason: ReviewReason | None = ReviewReason.low_confidence,
    description: str = "SPOTIFY",
    duplicate_of_id: uuid.UUID | None = None,
) -> Transaction:
    return Transaction(
        household_id=household_id,
        account_id=account_id,
        occurred_on=date(2026, 8, day),
        amount_minor_units=minor,
        currency="CAD",
        direction=TransactionDirection.debit,
        description=description,
        normalized_description=description.lower(),
        source=TransactionSource.upload,
        needs_review=needs_review,
        review_reason=reason if needs_review else None,
        duplicate_of_id=duplicate_of_id,
    )


async def walk(api_client, *, limit: int) -> list[dict]:
    """Every page of the queue, following cursors until there are none."""
    seen, cursor = [], None
    for _ in range(100):  # a bound, so a cursor that never ends fails loudly
        params = {"limit": limit} | ({"cursor": cursor} if cursor else {})
        page = (await api_client.get("/transactions/review", params=params)).json()
        seen.extend(page["rows"])
        cursor = page["next_cursor"]
        if cursor is None:
            return seen
    raise AssertionError("the queue never stopped paging")


class TestWhatIsInTheQueue:
    async def test_only_rows_needing_review_appear(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165573001")
        db_session.add_all(
            [
                flagged(household, account, day=1),
                flagged(household, account, day=2, needs_review=False),
            ]
        )
        await db_session.flush()

        rows = (await api_client.get("/transactions/review")).json()["rows"]

        assert [r["occurred_on"] for r in rows] == ["2026-08-01"]

    async def test_each_row_says_why_it_is_here(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165573002")
        db_session.add_all(
            [
                flagged(household, account, day=1, reason=ReviewReason.low_confidence),
                flagged(
                    household, account, day=2, reason=ReviewReason.unknown_category
                ),
            ]
        )
        await db_session.flush()

        rows = (await api_client.get("/transactions/review")).json()["rows"]

        assert {r["review_reason"] for r in rows} == {
            "low_confidence",
            "unknown_category",
        }

    async def test_a_suspected_duplicate_carries_what_it_matched(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165573003")
        original = flagged(
            household, account, day=3, needs_review=False, description="NETFLIX.COM"
        )
        db_session.add(original)
        await db_session.flush()
        db_session.add(
            flagged(
                household,
                account,
                day=3,
                reason=ReviewReason.suspected_duplicate,
                description="NETFLIX",
                duplicate_of_id=original.id,
            )
        )
        await db_session.flush()

        rows = (await api_client.get("/transactions/review")).json()["rows"]

        assert rows[0]["duplicate_of"]["id"] == str(original.id)
        assert rows[0]["duplicate_of"]["description"] == "NETFLIX.COM"
        # A decimal string, not a float and not minor units.
        assert rows[0]["duplicate_of"]["amount"] == "10.99"

    async def test_newest_statement_date_comes_first(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165573004")
        db_session.add_all([flagged(household, account, day=d) for d in (5, 20, 12)])
        await db_session.flush()

        rows = (await api_client.get("/transactions/review")).json()["rows"]

        assert [r["occurred_on"] for r in rows] == [
            "2026-08-20",
            "2026-08-12",
            "2026-08-05",
        ]


class TestHouseholdIsolation:
    async def test_another_household_s_rows_never_appear(self, api_client, db_session):
        theirs, their_account = await a_household(api_client, "+14165573010")
        db_session.add(flagged(theirs, their_account, day=1, description="THEIRS"))
        await db_session.flush()

        await a_household(api_client, "+14165573011")
        rows = (await api_client.get("/transactions/review")).json()["rows"]

        assert rows == []

    async def test_a_duplicate_pointer_cannot_read_another_household(
        self, api_client, db_session
    ):
        # A foreign key to `transaction` at large is not permission to read
        # whatever it points at. A stale or corrupted pointer must show nothing
        # rather than somebody else's statement line.
        theirs, their_account = await a_household(api_client, "+14165573012")
        secret = flagged(
            theirs, their_account, day=1, needs_review=False, description="PRIVATE"
        )
        db_session.add(secret)
        await db_session.flush()

        mine, my_account = await a_household(api_client, "+14165573013")
        db_session.add(
            flagged(
                mine,
                my_account,
                day=1,
                reason=ReviewReason.suspected_duplicate,
                duplicate_of_id=secret.id,
            )
        )
        await db_session.flush()

        rows = (await api_client.get("/transactions/review")).json()["rows"]

        assert rows[0]["duplicate_of"] is None


class TestPaging:
    async def test_every_row_is_seen_exactly_once(self, api_client, db_session):
        # Twelve rows across four dates, paged three at a time: most page
        # boundaries fall *inside* a run of same-date rows, which is exactly
        # where ordering on the date alone skips or repeats.
        household, account = await a_household(api_client, "+14165573020")
        made = [
            flagged(household, account, day=day, minor=1000 + i)
            for i, day in enumerate([1, 1, 1, 2, 2, 2, 2, 3, 3, 4, 4, 4])
        ]
        db_session.add_all(made)
        await db_session.flush()

        seen = await walk(api_client, limit=3)

        ids = [r["id"] for r in seen]
        assert len(ids) == len(set(ids)), "a row was shown twice"
        assert set(ids) == {str(t.id) for t in made}, "a row was skipped"

    async def test_a_row_resolved_mid_walk_does_not_shift_the_rest(
        self, api_client, db_session
    ):
        # The failure an offset cursor has and a keyset cursor does not: take a
        # row out of the queue between two page requests, and an offset shifts
        # everything after it up by one, so the row now sitting on the boundary
        # is never shown.
        household, account = await a_household(api_client, "+14165573021")
        made = [flagged(household, account, day=d) for d in range(1, 9)]
        db_session.add_all(made)
        await db_session.flush()

        first = (
            await api_client.get("/transactions/review", params={"limit": 3})
        ).json()
        shown = {r["id"] for r in first["rows"]}
        # The user resolves one row they have already seen.
        resolved = next(t for t in made if str(t.id) in shown)
        resolved.needs_review = False
        await db_session.flush()

        cursor, rest = first["next_cursor"], []
        while cursor:
            page = (
                await api_client.get(
                    "/transactions/review", params={"limit": 3, "cursor": cursor}
                )
            ).json()
            rest.extend(page["rows"])
            cursor = page["next_cursor"]

        unseen = {str(t.id) for t in made} - shown
        assert {r["id"] for r in rest} == unseen

    async def test_the_last_page_has_no_cursor(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165573022")
        db_session.add_all([flagged(household, account, day=d) for d in (1, 2)])
        await db_session.flush()

        page = (
            await api_client.get("/transactions/review", params={"limit": 2})
        ).json()

        assert len(page["rows"]) == 2
        assert page["next_cursor"] is None

    async def test_an_unreadable_cursor_is_a_400_not_a_500(self, api_client):
        await a_household(api_client, "+14165573023")

        response = await api_client.get(
            "/transactions/review", params={"cursor": "not-a-cursor"}
        )

        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_cursor"

    async def test_a_page_larger_than_the_limit_is_refused(self, api_client):
        await a_household(api_client, "+14165573024")

        response = await api_client.get(
            "/transactions/review", params={"limit": 10_000}
        )

        assert response.status_code == 422
