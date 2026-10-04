"""The review queue: what it shows, whose rows it shows, and how it pages.

The paging tests are the load-bearing ones. A queue exists so that every
uncertain row is seen by a person exactly once, and the two ways paging breaks
that promise — skipping a row, or showing one twice — are both invisible from
inside a single page.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from app.auth import AuthenticatedUser, current_user
from app.models.enums import ReviewReason, TransactionDirection, TransactionSource
from app.models.money import Transaction
from tests.conftest import requires_db

pytestmark = [
    # `integration` is what CI's database job selects (`pytest -m integration`).
    # Without it these ran nowhere: that job deselected them, and the fast job
    # has no database, so `requires_db` skipped them. Green, and untested.
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]


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
    category_id: uuid.UUID | None = None,
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
        category_id=category_id,
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


# --- Correcting a row -------------------------------------------------------


async def a_system_category(db_session) -> uuid.UUID:
    from sqlalchemy import select

    from app.models.categorization import Category

    result = await db_session.execute(
        select(Category.id).where(Category.household_id.is_(None)).limit(1)
    )
    return result.scalar_one()


async def an_import(db_session, household_id: uuid.UUID) -> uuid.UUID:
    from app.models.enums import SourceKind, StatementImportStatus
    from app.models.money import StatementImport

    record = StatementImport(
        household_id=household_id,
        source_kind=SourceKind.pdf_text,
        status=StatementImportStatus.awaiting_review,
    )
    db_session.add(record)
    await db_session.flush()
    return record.id


async def patch(api_client, row: Transaction, **fields):
    return await api_client.patch(f"/transactions/{row.id}", json=fields)


class TestCorrectingARow:
    async def test_an_answered_row_leaves_the_queue(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165573030")
        row = flagged(
            household,
            account,
            day=4,
            reason=ReviewReason.low_confidence,
            category_id=await a_system_category(db_session),
        )
        db_session.add(row)
        await db_session.flush()

        response = await patch(api_client, row, description="SPOTIFY PREMIUM")

        assert response.status_code == 200, response.text
        assert response.json()["transaction"]["needs_review"] is False
        assert response.json()["transaction"]["review_reason"] is None
        queue = (await api_client.get("/transactions/review")).json()["rows"]
        assert queue == []

    async def test_an_amount_goes_through_the_money_boundary(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165573031")
        row = flagged(household, account, day=4, minor=1099)
        db_session.add(row)
        await db_session.flush()

        response = await patch(api_client, row, amount="1234.50")

        assert response.status_code == 200, response.text
        # Stored as integer minor units, returned as a decimal string.
        await db_session.refresh(row)
        assert row.amount_minor_units == 123450
        assert response.json()["transaction"]["amount"] == "1234.50"

    async def test_a_grouped_amount_is_refused_rather_than_guessed(
        self, api_client, db_session
    ):
        # The API takes a plain decimal string. Accepting "1,234.50" would mean
        # also deciding what "1.234,50" means, which is a locale question the
        # server has no business guessing on someone's money — the client
        # formats for display and sends the number.
        household, account = await a_household(api_client, "+14165573035")
        row = flagged(household, account, day=4, minor=1099)
        db_session.add(row)
        await db_session.flush()

        response = await patch(api_client, row, amount="1,234.50")

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_amount"
        await db_session.refresh(row)
        assert row.amount_minor_units == 1099

    async def test_a_new_description_re_derives_the_key_and_merchant(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165573032")
        row = flagged(household, account, day=4, description="SPTFY")
        db_session.add(row)
        await db_session.flush()

        await patch(api_client, row, description="SPOTIFY P3A4B5C6")

        await db_session.refresh(row)
        assert row.normalized_description != "sptfy"
        assert row.merchant is not None and "spotify" in row.merchant.lower()

    async def test_a_merchant_the_user_names_wins_over_the_derived_one(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165573033")
        row = flagged(household, account, day=4, description="SQ *CAFE 123")
        db_session.add(row)
        await db_session.flush()

        await patch(
            api_client, row, description="SQ *CAFE 123 TORONTO", merchant="Cafe Luna"
        )

        await db_session.refresh(row)
        assert row.merchant == "Cafe Luna"

    async def test_answering_a_duplicate_question_drops_the_pointer(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165573034")
        original = flagged(household, account, day=3, needs_review=False)
        db_session.add(original)
        await db_session.flush()
        suspect = flagged(
            household,
            account,
            day=3,
            minor=2000,
            reason=ReviewReason.suspected_duplicate,
            duplicate_of_id=original.id,
        )
        db_session.add(suspect)
        await db_session.flush()

        await patch(api_client, suspect, description="A DIFFERENT PURCHASE")

        await db_session.refresh(suspect)
        assert suspect.duplicate_of_id is None


class TestAnEditThatWouldDuplicate:
    async def test_changing_the_amount_onto_an_existing_row_is_refused(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165573040")
        existing = flagged(household, account, day=6, minor=5000, needs_review=False)
        typo = flagged(household, account, day=6, minor=500)
        db_session.add_all([existing, typo])
        await db_session.flush()

        response = await patch(api_client, typo, amount="50.00")

        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "would_duplicate"
        assert detail["duplicate_of"] == str(existing.id)

    async def test_a_refused_edit_changes_nothing(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165573041")
        existing = flagged(household, account, day=6, minor=5000, needs_review=False)
        typo = flagged(household, account, day=6, minor=500)
        db_session.add_all([existing, typo])
        await db_session.flush()

        await patch(api_client, typo, amount="50.00")

        # Still in the queue, still the old figure: the user has to decide
        # what this row is, and a half-applied edit would decide for them.
        queue = (await api_client.get("/transactions/review")).json()["rows"]
        assert [r["id"] for r in queue] == [str(typo.id)]
        assert queue[0]["amount"] == "5.00"

    async def test_changing_the_date_onto_an_existing_row_is_refused(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165573042")
        existing = flagged(household, account, day=6, needs_review=False)
        typo = flagged(household, account, day=16)
        db_session.add_all([existing, typo])
        await db_session.flush()

        response = await patch(api_client, typo, occurred_on="2026-08-06")

        assert response.status_code == 409, response.text

    async def test_a_same_day_same_amount_row_with_another_description_is_allowed(
        self, api_client, db_session
    ):
        # Two $20 withdrawals in one afternoon are two withdrawals. Import
        # flags that shape for a person to compare; an edit must not be
        # refused for it, or ordinary corrections become impossible.
        household, account = await a_household(api_client, "+14165573043")
        existing = flagged(
            household,
            account,
            day=9,
            minor=2000,
            needs_review=False,
            description="ATM WITHDRAWAL KING ST",
        )
        other = flagged(
            household, account, day=9, minor=1500, description="ATM WITHDRAWAL QUEEN ST"
        )
        db_session.add_all([existing, other])
        await db_session.flush()

        response = await patch(api_client, other, amount="20.00")

        assert response.status_code == 200, response.text


class TestWhoseRowItIs:
    async def test_another_household_s_row_is_not_found(self, api_client, db_session):
        theirs, their_account = await a_household(api_client, "+14165573050")
        row = flagged(theirs, their_account, day=2)
        db_session.add(row)
        await db_session.flush()

        await a_household(api_client, "+14165573051")
        response = await patch(api_client, row, description="MINE NOW")

        # 404, not 403: a 403 would confirm the id exists.
        assert response.status_code == 404
        await db_session.refresh(row)
        assert row.description == "SPOTIFY"


class TestFilingIntoACategory:
    async def test_a_system_category_is_accepted(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165573060")
        row = flagged(household, account, day=2, reason=ReviewReason.unknown_category)
        db_session.add(row)
        await db_session.flush()
        category = await a_system_category(db_session)

        response = await patch(api_client, row, category_id=str(category))

        assert response.status_code == 200, response.text
        assert response.json()["transaction"]["category_id"] == str(category)

    async def test_another_household_s_category_is_unknown(
        self, api_client, db_session
    ):
        from app.models.categorization import Category

        theirs, _ = await a_household(api_client, "+14165573061")
        private = Category(household_id=theirs, slug="their-thing", name="Theirs")
        db_session.add(private)
        await db_session.flush()

        mine, account = await a_household(api_client, "+14165573062")
        row = flagged(mine, account, day=2)
        db_session.add(row)
        await db_session.flush()

        response = await patch(api_client, row, category_id=str(private.id))

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "unknown_category"


class TestWhatIsNotACorrection:
    async def test_an_empty_patch_is_refused(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165573070")
        row = flagged(household, account, day=2)
        db_session.add(row)
        await db_session.flush()

        response = await patch(api_client, row)

        assert response.status_code == 422
        await db_session.refresh(row)
        assert row.needs_review is True

    async def test_a_negative_amount_is_refused(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165573071")
        row = flagged(household, account, day=2)
        db_session.add(row)
        await db_session.flush()

        response = await patch(api_client, row, amount="-5.00")

        assert response.status_code == 422

    async def test_a_future_date_is_refused(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165573072")
        row = flagged(household, account, day=2)
        db_session.add(row)
        await db_session.flush()

        response = await patch(api_client, row, occurred_on="2999-01-01")

        assert response.status_code == 422


class TestFinishingAnImport:
    async def test_resolving_the_last_row_finishes_the_import(
        self, api_client, db_session
    ):
        from app.models.money import StatementImport

        household, account = await a_household(api_client, "+14165573080")
        import_id = await an_import(db_session, household)
        row = flagged(
            household, account, day=2, category_id=await a_system_category(db_session)
        )
        row.statement_import_id = import_id
        db_session.add(row)
        await db_session.flush()

        response = await patch(api_client, row, description="SPOTIFY")

        assert response.json()["import_finished"] is True
        record = await db_session.get(StatementImport, import_id)
        await db_session.refresh(record)
        assert record.confirmed_at is not None

    async def test_an_import_with_a_row_outstanding_is_not_finished(
        self, api_client, db_session
    ):
        from app.models.money import StatementImport

        household, account = await a_household(api_client, "+14165573081")
        import_id = await an_import(db_session, household)
        first = flagged(household, account, day=2)
        second = flagged(household, account, day=3)
        for row in (first, second):
            row.statement_import_id = import_id
        db_session.add_all([first, second])
        await db_session.flush()

        response = await patch(api_client, first, description="SPOTIFY")

        assert response.json()["import_finished"] is False
        record = await db_session.get(StatementImport, import_id)
        await db_session.refresh(record)
        assert record.confirmed_at is None


# --- Confirming rows as they are --------------------------------------------


async def still_waiting(db_session, *rows: Transaction) -> list[bool]:
    for row in rows:
        await db_session.refresh(row)
    return [row.needs_review for row in rows]


class TestConfirmingOne:
    async def test_a_confirmed_row_leaves_the_queue_unchanged(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165575001")
        row = flagged(
            household,
            account,
            day=4,
            minor=4242,
            description="COSTCO",
            category_id=await a_system_category(db_session),
        )
        db_session.add(row)
        await db_session.flush()

        response = await api_client.post(f"/transactions/{row.id}/confirm")

        assert response.status_code == 200, response.text
        await db_session.refresh(row)
        assert row.needs_review is False
        assert row.review_reason is None
        # Accepted as extracted: nothing about the row itself moved.
        assert (row.amount_minor_units, row.description) == (4242, "COSTCO")

    async def test_confirming_twice_is_fine(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165575002")
        row = flagged(household, account, day=4)
        db_session.add(row)
        await db_session.flush()

        first = await api_client.post(f"/transactions/{row.id}/confirm")
        second = await api_client.post(f"/transactions/{row.id}/confirm")

        assert (first.status_code, second.status_code) == (200, 200)

    async def test_confirming_a_suspected_duplicate_drops_the_pointer(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165575003")
        original = flagged(household, account, day=3, needs_review=False)
        db_session.add(original)
        await db_session.flush()
        suspect = flagged(
            household,
            account,
            day=3,
            minor=2000,
            reason=ReviewReason.suspected_duplicate,
            duplicate_of_id=original.id,
        )
        db_session.add(suspect)
        await db_session.flush()

        await api_client.post(f"/transactions/{suspect.id}/confirm")

        await db_session.refresh(suspect)
        assert suspect.duplicate_of_id is None

    async def test_another_household_s_row_is_not_found(self, api_client, db_session):
        theirs, their_account = await a_household(api_client, "+14165575004")
        row = flagged(theirs, their_account, day=4)
        db_session.add(row)
        await db_session.flush()

        await a_household(api_client, "+14165575005")
        response = await api_client.post(f"/transactions/{row.id}/confirm")

        assert response.status_code == 404
        assert await still_waiting(db_session, row) == [True]

    async def test_confirming_the_last_row_finishes_the_import(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165575006")
        import_id = await an_import(db_session, household)
        row = flagged(
            household, account, day=4, category_id=await a_system_category(db_session)
        )
        row.statement_import_id = import_id
        db_session.add(row)
        await db_session.flush()

        response = await api_client.post(f"/transactions/{row.id}/confirm")

        assert response.json()["import_finished"] is True


class TestConfirmingMany:
    async def test_every_row_listed_leaves_the_queue(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165575010")
        category = await a_system_category(db_session)
        rows = [
            flagged(household, account, day=d, category_id=category) for d in (1, 2, 3)
        ]
        db_session.add_all(rows)
        await db_session.flush()

        response = await api_client.post(
            "/transactions/confirm", json={"ids": [str(r.id) for r in rows]}
        )

        assert response.status_code == 200, response.text
        assert response.json()["confirmed"] == 3
        assert await still_waiting(db_session, *rows) == [False, False, False]

    async def test_a_retry_is_harmless_and_reports_nothing_new(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165575011")
        rows = [flagged(household, account, day=d) for d in (1, 2)]
        db_session.add_all(rows)
        await db_session.flush()
        ids = {"ids": [str(r.id) for r in rows]}

        await api_client.post("/transactions/confirm", json=ids)
        again = await api_client.post("/transactions/confirm", json=ids)

        assert again.status_code == 200
        assert again.json()["confirmed"] == 0

    async def test_an_id_listed_twice_counts_once(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165575012")
        row = flagged(
            household, account, day=1, category_id=await a_system_category(db_session)
        )
        db_session.add(row)
        await db_session.flush()

        response = await api_client.post(
            "/transactions/confirm", json={"ids": [str(row.id), str(row.id)]}
        )

        assert response.status_code == 200, response.text
        assert response.json()["confirmed"] == 1

    @pytest.mark.parametrize("position", ["first", "middle", "last"])
    async def test_one_foreign_id_anywhere_confirms_nothing(
        self, api_client, db_session, position
    ):
        # The acceptance criterion: partial application across households is
        # not an acceptable outcome. Tried at each end and in the middle,
        # because an implementation that writes as it goes fails differently
        # depending on where the bad id sits — at the end it has already
        # written everything before it.
        base = {"first": "+1416557502", "middle": "+1416557503", "last": "+1416557504"}
        theirs, their_account = await a_household(api_client, base[position] + "0")
        foreign = flagged(theirs, their_account, day=9)
        db_session.add(foreign)
        await db_session.flush()

        mine, my_account = await a_household(api_client, base[position] + "1")
        own = [flagged(mine, my_account, day=d) for d in (1, 2)]
        db_session.add_all(own)
        await db_session.flush()

        ids = [str(r.id) for r in own]
        ids.insert({"first": 0, "middle": 1, "last": 2}[position], str(foreign.id))
        response = await api_client.post("/transactions/confirm", json={"ids": ids})

        assert response.status_code == 404
        assert await still_waiting(db_session, *own) == [True, True]
        assert await still_waiting(db_session, foreign) == [True]

    async def test_an_id_that_exists_nowhere_confirms_nothing(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165575050")
        row = flagged(household, account, day=1)
        db_session.add(row)
        await db_session.flush()

        response = await api_client.post(
            "/transactions/confirm",
            json={"ids": [str(row.id), str(uuid.uuid4())]},
        )

        assert response.status_code == 404
        assert await still_waiting(db_session, row) == [True]

    async def test_an_empty_list_is_refused(self, api_client):
        await a_household(api_client, "+14165575051")

        response = await api_client.post("/transactions/confirm", json={"ids": []})

        assert response.status_code == 422

    async def test_confirming_the_rest_of_an_import_finishes_it(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165575052")
        import_id = await an_import(db_session, household)
        category = await a_system_category(db_session)
        rows = [
            flagged(household, account, day=d, category_id=category) for d in (1, 2)
        ]
        for row in rows:
            row.statement_import_id = import_id
        db_session.add_all(rows)
        await db_session.flush()

        response = await api_client.post(
            "/transactions/confirm", json={"ids": [str(r.id) for r in rows]}
        )

        assert response.json()["imports_finished"] == [str(import_id)]

    async def test_an_import_with_a_row_left_is_not_finished(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165575053")
        import_id = await an_import(db_session, household)
        rows = [flagged(household, account, day=d) for d in (1, 2)]
        for row in rows:
            row.statement_import_id = import_id
        db_session.add_all(rows)
        await db_session.flush()

        response = await api_client.post(
            "/transactions/confirm", json={"ids": [str(rows[0].id)]}
        )

        assert response.json()["imports_finished"] == []

    async def test_confirming_teaches_the_categorizer_nothing(
        self, api_client, db_session
    ):
        # Accepting the categorizer's answer is not a correction of it.
        from sqlalchemy import func, select

        from app.models.categorization import CategoryCorrection

        household, account = await a_household(api_client, "+14165575054")
        row = flagged(household, account, day=1)
        row.merchant = "Spotify"
        db_session.add(row)
        await db_session.flush()

        await api_client.post("/transactions/confirm", json={"ids": [str(row.id)]})

        count = await db_session.execute(
            select(func.count())
            .select_from(CategoryCorrection)
            .where(CategoryCorrection.household_id == household)
        )
        assert count.scalar_one() == 0


# --- Deleting a row that was never a transaction -----------------------------


async def exists(db_session, row_id: uuid.UUID) -> bool:
    from sqlalchemy import select

    result = await db_session.execute(
        select(Transaction.id).where(Transaction.id == row_id)
    )
    return result.scalar_one_or_none() is not None


class TestDeleting:
    async def test_a_row_the_user_rejects_is_gone(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165576001")
        row = flagged(household, account, day=4, description="OPENING BALANCE")
        db_session.add(row)
        await db_session.flush()
        row_id = row.id

        response = await api_client.delete(f"/transactions/{row_id}")

        assert response.status_code == 200, response.text
        assert not await exists(db_session, row_id)

    async def test_another_household_s_row_survives(self, api_client, db_session):
        theirs, their_account = await a_household(api_client, "+14165576002")
        row = flagged(theirs, their_account, day=4)
        db_session.add(row)
        await db_session.flush()

        await a_household(api_client, "+14165576003")
        response = await api_client.delete(f"/transactions/{row.id}")

        # 404, the same answer as an id that never existed.
        assert response.status_code == 404
        assert await exists(db_session, row.id)

    async def test_deleting_twice_says_it_is_already_gone(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165576004")
        row = flagged(household, account, day=4)
        db_session.add(row)
        await db_session.flush()

        await api_client.delete(f"/transactions/{row.id}")
        again = await api_client.delete(f"/transactions/{row.id}")

        assert again.status_code == 404

    async def test_a_confirmed_row_can_be_deleted_too(self, api_client, db_session):
        # A bogus row found after confirming it is just as bogus.
        household, account = await a_household(api_client, "+14165576005")
        row = flagged(household, account, day=4, needs_review=False)
        db_session.add(row)
        await db_session.flush()
        row_id = row.id

        response = await api_client.delete(f"/transactions/{row_id}")

        assert response.status_code == 200
        assert not await exists(db_session, row_id)

    async def test_deleting_the_last_waiting_row_finishes_the_import(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165576006")
        import_id = await an_import(db_session, household)
        row = flagged(household, account, day=4)
        row.statement_import_id = import_id
        db_session.add(row)
        await db_session.flush()

        response = await api_client.delete(f"/transactions/{row.id}")

        assert response.json()["import_finished"] is True

    async def test_deleting_a_confirmed_row_finishes_nothing(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165576007")
        import_id = await an_import(db_session, household)
        done = flagged(household, account, day=4, needs_review=False)
        waiting = flagged(household, account, day=5)
        for row in (done, waiting):
            row.statement_import_id = import_id
        db_session.add_all([done, waiting])
        await db_session.flush()

        response = await api_client.delete(f"/transactions/{done.id}")

        assert response.json()["import_finished"] is False


class TestDeletingReachesNoFurtherThanTheRow:
    async def test_a_suspected_duplicate_of_it_survives(self, api_client, db_session):
        # The user deleted the row a suspect was compared against. The suspect
        # is its own transaction and has not been answered: it must not be
        # swept away with the row it happened to resemble.
        household, account = await a_household(api_client, "+14165576010")
        original = flagged(household, account, day=3, needs_review=False)
        db_session.add(original)
        await db_session.flush()
        suspect = flagged(
            household,
            account,
            day=3,
            minor=2000,
            reason=ReviewReason.suspected_duplicate,
            duplicate_of_id=original.id,
        )
        db_session.add(suspect)
        await db_session.flush()

        await api_client.delete(f"/transactions/{original.id}")

        assert await exists(db_session, suspect.id)
        await db_session.refresh(suspect)
        assert suspect.needs_review is True
        assert suspect.duplicate_of_id is None

    async def test_a_rule_learned_from_it_keeps_teaching(self, api_client, db_session):
        # The rule is about the merchant, not about this line. Deleting the
        # line it was learned from must not unlearn it.
        from sqlalchemy import select

        from app.models.categorization import Category, CategoryCorrection

        household, account = await a_household(api_client, "+14165576011")
        category = (
            await db_session.execute(
                select(Category.id).where(Category.household_id.is_(None)).limit(1)
            )
        ).scalar_one()
        row = flagged(household, account, day=3)
        row.merchant = "Spotify"
        db_session.add(row)
        await db_session.flush()
        await patch(api_client, row, category_id=str(category))

        await api_client.delete(f"/transactions/{row.id}")

        rule = (
            await db_session.execute(
                select(CategoryCorrection).where(
                    CategoryCorrection.household_id == household
                )
            )
        ).scalar_one()
        await db_session.refresh(rule)
        assert rule.merchant_pattern == "spotify"
        assert rule.transaction_id is None


class TestAnUncategorizedRowStaysUntilFiled:
    """Answering a row's other question never releases it without a category.

    A row carries one reason, so an uncategorized suspected duplicate asked
    only "is this the same as that?" — and answering it used to let the row go
    with no category, which M4's budgets would then silently miss (#38 review).
    """

    async def test_confirming_a_suspected_duplicate_asks_for_a_category_next(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165576101")
        original = flagged(household, account, day=5, needs_review=False)
        db_session.add(original)
        await db_session.flush()
        row = flagged(
            household,
            account,
            day=5,
            description="SPOTIFY USA",
            reason=ReviewReason.suspected_duplicate,
            duplicate_of_id=original.id,
        )
        db_session.add(row)
        await db_session.flush()

        response = await api_client.post(f"/transactions/{row.id}/confirm")

        assert response.status_code == 200, response.text
        body = response.json()["transaction"]
        # The duplicate question is answered — "no" — so the pointer goes…
        assert body["duplicate_of"] is None
        # …but the row stays, now asking the question it was hiding.
        assert body["needs_review"] is True
        assert body["review_reason"] == "unknown_category"
        await db_session.refresh(row)
        assert (row.needs_review, row.duplicate_of_id) == (True, None)

    async def test_confirming_again_does_not_release_it_either(
        self, api_client, db_session
    ):
        # A retried confirm is not a person choosing "no category".
        household, account = await a_household(api_client, "+14165576102")
        row = flagged(household, account, day=6, reason=ReviewReason.unknown_category)
        db_session.add(row)
        await db_session.flush()
        ids = {"ids": [str(row.id)]}

        first = await api_client.post("/transactions/confirm", json=ids)
        again = await api_client.post("/transactions/confirm", json=ids)

        assert (first.json()["confirmed"], again.json()["confirmed"]) == (0, 0)
        assert await still_waiting(db_session, row) == [True]

    async def test_an_import_is_not_finished_while_one_waits_for_a_category(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165576103")
        import_id = await an_import(db_session, household)
        row = flagged(household, account, day=7)
        row.statement_import_id = import_id
        db_session.add(row)
        await db_session.flush()

        response = await api_client.post(f"/transactions/{row.id}/confirm")

        assert response.json()["import_finished"] is False

    async def test_filing_it_is_what_lets_it_go(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165576104")
        row = flagged(household, account, day=8, reason=ReviewReason.unknown_category)
        db_session.add(row)
        await db_session.flush()

        response = await patch(
            api_client, row, category_id=str(await a_system_category(db_session))
        )

        assert response.json()["transaction"]["needs_review"] is False
        assert await still_waiting(db_session, row) == [False]


class TestListingEverythingNotJustTheQueue:
    """`GET /transactions`, which exists so a person can see what the model
    filed with confidence. Until it did, a row the model was sure about was
    saved and shown to nobody."""

    async def test_it_returns_settled_rows_as_well_as_flagged_ones(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165573801")
        db_session.add_all(
            [
                flagged(household, account, day=1),
                flagged(household, account, day=2, needs_review=False),
                flagged(household, account, day=3, needs_review=False),
            ]
        )
        await db_session.commit()

        everything = (await api_client.get("/transactions")).json()["rows"]
        queue = (await api_client.get("/transactions/review")).json()["rows"]

        assert len(everything) == 3
        assert len(queue) == 1, "the queue itself must not have changed"

    async def test_each_row_says_which_kind_it_is(self, api_client, db_session):
        """Without this the client has to infer it from `review_reason` being
        null, which is a guess: a settled row can still carry the reason it
        once needed looking at."""
        household, account = await a_household(api_client, "+14165573802")
        db_session.add_all(
            [
                flagged(household, account, day=1),
                flagged(household, account, day=2, needs_review=False),
            ]
        )
        await db_session.commit()

        rows = (await api_client.get("/transactions")).json()["rows"]

        assert sorted(row["needs_review"] for row in rows) == [False, True]

    async def test_needs_review_false_selects_only_the_settled(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165573803")
        db_session.add_all(
            [
                flagged(household, account, day=1),
                flagged(household, account, day=2, needs_review=False),
            ]
        )
        await db_session.commit()

        rows = (
            await api_client.get("/transactions", params={"needs_review": "false"})
        ).json()["rows"]

        assert [row["needs_review"] for row in rows] == [False]

    async def test_an_import_id_scopes_the_page_to_that_import(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165573804")
        from app.models.money import StatementImport

        wanted = StatementImport(household_id=household, source_kind="pdf_text")
        other = StatementImport(household_id=household, source_kind="pdf_text")
        db_session.add_all([wanted, other])
        await db_session.flush()

        mine = flagged(household, account, day=1, needs_review=False)
        mine.statement_import_id = wanted.id
        theirs = flagged(household, account, day=2, needs_review=False)
        theirs.statement_import_id = other.id
        typed_in = flagged(household, account, day=3, needs_review=False)
        db_session.add_all([mine, theirs, typed_in])
        await db_session.commit()

        rows = (
            await api_client.get(
                "/transactions", params={"statement_import_id": str(wanted.id)}
            )
        ).json()["rows"]

        assert [row["id"] for row in rows] == [str(mine.id)]

    async def test_another_household_s_import_returns_nothing_not_a_404(
        self, api_client, db_session
    ):
        """An empty page, not a 404. A 404 that differs from an empty page
        tells the caller whether an import id exists, which is a membership
        oracle for ids they do not own."""
        from app.models.money import StatementImport

        stranger, stranger_account = await a_household(api_client, "+14165573805")
        theirs = StatementImport(household_id=stranger, source_kind="pdf_text")
        db_session.add(theirs)
        await db_session.flush()
        row = flagged(stranger, stranger_account, day=1, needs_review=False)
        row.statement_import_id = theirs.id
        db_session.add(row)
        await db_session.commit()

        await a_household(api_client, "+14165573806")
        response = await api_client.get(
            "/transactions", params={"statement_import_id": str(theirs.id)}
        )

        assert response.status_code == 200
        assert response.json()["rows"] == []

    async def test_it_never_reaches_another_household(self, api_client, db_session):
        stranger, stranger_account = await a_household(api_client, "+14165573807")
        db_session.add(flagged(stranger, stranger_account, day=1, needs_review=False))
        await db_session.commit()

        mine, my_account = await a_household(api_client, "+14165573808")
        db_session.add(flagged(mine, my_account, day=2, needs_review=False))
        await db_session.commit()

        rows = (await api_client.get("/transactions")).json()["rows"]

        assert len(rows) == 1
        assert rows[0]["account_id"] == str(my_account)


class TestBrowsingByMonth:
    """`GET /transactions?month=` — the other way a person browses what they
    have, beside one statement at a time."""

    async def test_only_the_month_asked_for_comes_back(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165573901")
        db_session.add_all(
            [
                flagged(household, account, day=31, needs_review=False),
                flagged(household, account, day=1, needs_review=False),
            ]
        )
        # One in July, which must not appear in August.
        july = flagged(household, account, day=15, needs_review=False)
        july.occurred_on = date(2026, 7, 15)
        db_session.add(july)
        await db_session.commit()

        rows = (
            await api_client.get("/transactions", params={"month": "2026-08"})
        ).json()["rows"]

        assert len(rows) == 2
        assert {row["occurred_on"] for row in rows} == {"2026-08-31", "2026-08-01"}

    async def test_both_ends_of_the_month_are_inside_it(self, api_client, db_session):
        """`occurred_on` is a date, so a half-open range drops the 31st."""
        household, account = await a_household(api_client, "+14165573902")
        db_session.add_all(
            [
                flagged(household, account, day=1, needs_review=False),
                flagged(household, account, day=31, needs_review=False),
            ]
        )
        await db_session.commit()

        rows = (
            await api_client.get("/transactions", params={"month": "2026-08"})
        ).json()["rows"]

        assert len(rows) == 2

    async def test_a_month_with_nothing_in_it_is_an_empty_page(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165573903")
        db_session.add(flagged(household, account, day=2, needs_review=False))
        await db_session.commit()

        response = await api_client.get("/transactions", params={"month": "2026-01"})

        assert response.status_code == 200
        assert response.json()["rows"] == []

    async def test_a_malformed_month_is_refused_rather_than_ignored(self, api_client):
        """Ignoring it would quietly return every row the household has, which
        is the opposite of what was asked for."""
        await a_household(api_client, "+14165573904")

        for bad in ("2026-13", "august", "2026", ""):
            response = await api_client.get("/transactions", params={"month": bad})
            assert response.status_code == 422, bad
            assert response.json()["detail"]["code"] == "invalid_month"

    async def test_a_month_and_a_statement_narrow_together(
        self, api_client, db_session
    ):
        from app.models.money import StatementImport

        household, account = await a_household(api_client, "+14165573905")
        wanted = StatementImport(household_id=household, source_kind="pdf_text")
        db_session.add(wanted)
        await db_session.flush()

        inside = flagged(household, account, day=2, needs_review=False)
        inside.statement_import_id = wanted.id
        outside = flagged(household, account, day=3, needs_review=False)
        outside.occurred_on = date(2026, 7, 3)
        outside.statement_import_id = wanted.id
        db_session.add_all([inside, outside])
        await db_session.commit()

        rows = (
            await api_client.get(
                "/transactions",
                params={"month": "2026-08", "statement_import_id": str(wanted.id)},
            )
        ).json()["rows"]

        assert [row["id"] for row in rows] == [str(inside.id)]

    async def test_the_queue_is_not_month_scoped(self, api_client, db_session):
        """`/transactions/review` answers "what still needs me", which is not a
        question about a month."""
        household, account = await a_household(api_client, "+14165573906")
        old = flagged(household, account, day=4)
        old.occurred_on = date(2025, 1, 4)
        db_session.add(old)
        await db_session.commit()

        rows = (await api_client.get("/transactions/review")).json()["rows"]

        assert len(rows) == 1


class TestListingTheStatements:
    """`GET /statements` — so "show me what came from the May statement" is a
    question that can be asked at all."""

    async def test_imports_come_back_newest_first_with_their_counts(
        self, api_client, db_session
    ):
        from app.models.money import StatementImport

        household, account = await a_household(api_client, "+14165573910")
        # Distinct timestamps, because that is what production has: each import
        # is its own request. Written in one transaction they would share a
        # created_at exactly — Postgres `now()` is transaction time — and two
        # rows with the same instant have no chronological order to recover,
        # since the ids are random rather than time-ordered.
        older = StatementImport(
            household_id=household,
            source_kind="pdf_text",
            created_at=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
        )
        newer = StatementImport(
            household_id=household,
            source_kind="ocr",
            created_at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
        )
        db_session.add_all([older, newer])
        await db_session.flush()

        for day, needs in ((1, True), (2, False), (3, False)):
            row = flagged(household, account, day=day, needs_review=needs)
            row.statement_import_id = newer.id
            db_session.add(row)
        await db_session.commit()

        body = (await api_client.get("/statements")).json()

        assert [item["id"] for item in body["imports"]] == [
            str(newer.id),
            str(older.id),
        ]
        assert body["imports"][0]["saved"] == 3
        assert body["imports"][0]["needs_review"] == 1
        assert body["imports"][1]["saved"] == 0, "an import with no rows still lists"

    async def test_each_import_says_when_it_happened(self, api_client, db_session):
        """A list of statements is unreadable without it — "pdf_text, 24 rows"
        three times over names nothing a person can pick from."""
        from app.models.money import StatementImport

        household, _ = await a_household(api_client, "+14165573911")
        db_session.add(StatementImport(household_id=household, source_kind="pdf_text"))
        await db_session.commit()

        body = (await api_client.get("/statements")).json()

        assert body["imports"][0]["created_at"]

    async def test_another_household_s_imports_are_not_listed(
        self, api_client, db_session
    ):
        from app.models.money import StatementImport

        stranger, _ = await a_household(api_client, "+14165573912")
        db_session.add(StatementImport(household_id=stranger, source_kind="pdf_text"))
        await db_session.commit()

        await a_household(api_client, "+14165573913")
        body = (await api_client.get("/statements")).json()

        assert body["imports"] == []

    async def test_a_household_with_no_imports_gets_an_empty_list(self, api_client):
        await a_household(api_client, "+14165573914")

        response = await api_client.get("/statements")

        assert response.status_code == 200
        assert response.json()["imports"] == []
