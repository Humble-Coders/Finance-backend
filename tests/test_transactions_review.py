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
        row = flagged(household, account, day=4, reason=ReviewReason.low_confidence)
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
        row = flagged(household, account, day=2)
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
        row = flagged(household, account, day=4, minor=4242, description="COSTCO")
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
        row = flagged(household, account, day=4)
        row.statement_import_id = import_id
        db_session.add(row)
        await db_session.flush()

        response = await api_client.post(f"/transactions/{row.id}/confirm")

        assert response.json()["import_finished"] is True


class TestConfirmingMany:
    async def test_every_row_listed_leaves_the_queue(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165575010")
        rows = [flagged(household, account, day=d) for d in (1, 2, 3)]
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
        row = flagged(household, account, day=1)
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
        rows = [flagged(household, account, day=d) for d in (1, 2)]
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
