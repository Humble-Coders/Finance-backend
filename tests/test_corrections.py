"""What a category correction teaches, and who it is allowed to teach.

Three promises, each with a test built to fail if it is broken: one rule per
merchant per household; the rule reaches rows still waiting and never rows the
user already answered; and nothing one household teaches is visible to another.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.categorization import Category, CategoryCorrection
from app.models.enums import ReviewReason
from app.services.categorization import _examples
from tests.conftest import requires_db
from tests.test_transactions_review import a_household, flagged, patch

pytestmark = [pytest.mark.asyncio(loop_scope="session"), requires_db]


async def two_system_categories(db_session) -> tuple[uuid.UUID, uuid.UUID]:
    result = await db_session.execute(
        select(Category.id)
        .where(Category.household_id.is_(None))
        .order_by(Category.slug)
        .limit(2)
    )
    first, second = result.scalars().all()
    return first, second


def at(household, account, *, day, merchant, needs_review=True, category=None):
    row = flagged(
        household,
        account,
        day=day,
        minor=1000 + day,
        needs_review=needs_review,
        reason=ReviewReason.unknown_category if needs_review else None,
        description=merchant.upper(),
    )
    row.merchant = merchant
    row.category_id = category
    return row


async def rules(db_session, household) -> list[CategoryCorrection]:
    result = await db_session.execute(
        select(CategoryCorrection).where(CategoryCorrection.household_id == household)
    )
    return list(result.scalars().all())


class TestOneRulePerMerchant:
    async def test_a_correction_writes_exactly_one_rule(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165574001")
        wrong, right = await two_system_categories(db_session)
        row = at(household, account, day=3, merchant="Spotify", category=wrong)
        db_session.add(row)
        await db_session.flush()

        response = await patch(api_client, row, category_id=str(right))

        assert response.status_code == 200, response.text
        assert response.json()["rule_recorded"] is True
        [rule] = await rules(db_session, household)
        assert rule.merchant_pattern == "spotify"
        assert rule.predicted_category_id == wrong
        assert rule.corrected_category_id == right

    async def test_correcting_the_same_merchant_again_replaces_the_rule(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165574002")
        first, second = await two_system_categories(db_session)
        one = at(household, account, day=3, merchant="Spotify")
        two = at(household, account, day=4, merchant="Spotify")
        db_session.add_all([one, two])
        await db_session.flush()

        await patch(api_client, one, category_id=str(first))
        await patch(api_client, two, category_id=str(second))

        # One rule saying the latest answer — not two a later reader must pick
        # between, and not a prompt fed both at once.
        [rule] = await rules(db_session, household)
        await db_session.refresh(rule)
        assert rule.corrected_category_id == second

    async def test_case_and_spacing_do_not_make_a_second_merchant(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165574003")
        first, second = await two_system_categories(db_session)
        one = at(household, account, day=3, merchant="Cafe Luna")
        two = at(household, account, day=4, merchant="cafe   LUNA")
        db_session.add_all([one, two])
        await db_session.flush()

        await patch(api_client, one, category_id=str(first))
        await patch(api_client, two, category_id=str(second))

        assert len(await rules(db_session, household)) == 1

    async def test_agreeing_with_the_categorizer_teaches_nothing(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165574004")
        right, _ = await two_system_categories(db_session)
        row = at(household, account, day=3, merchant="Spotify", category=right)
        db_session.add(row)
        await db_session.flush()

        response = await patch(api_client, row, category_id=str(right))

        assert response.json()["rule_recorded"] is False
        assert await rules(db_session, household) == []

    async def test_a_row_with_no_merchant_is_corrected_without_a_rule(
        self, api_client, db_session
    ):
        household, account = await a_household(api_client, "+14165574005")
        right, _ = await two_system_categories(db_session)
        row = at(household, account, day=3, merchant="x")
        row.merchant = None
        db_session.add(row)
        await db_session.flush()

        response = await patch(api_client, row, category_id=str(right))

        assert response.status_code == 200, response.text
        assert response.json()["transaction"]["category_id"] == str(right)
        assert response.json()["rule_recorded"] is False
        assert await rules(db_session, household) == []


class TestApplyingTheRuleNow:
    async def test_waiting_rows_move_and_confirmed_rows_do_not(
        self, api_client, db_session
    ):
        # The acceptance criterion's "one of each": a Spotify row still in the
        # queue, and a Spotify row the user already answered. Only the first
        # may move — the second was the user's own decision about that row.
        household, account = await a_household(api_client, "+14165574010")
        old, new = await two_system_categories(db_session)
        corrected = at(household, account, day=3, merchant="Spotify", category=old)
        waiting = at(household, account, day=4, merchant="Spotify", category=old)
        confirmed = at(
            household,
            account,
            day=5,
            merchant="Spotify",
            needs_review=False,
            category=old,
        )
        db_session.add_all([corrected, waiting, confirmed])
        await db_session.flush()

        response = await patch(api_client, corrected, category_id=str(new))

        assert response.json()["recategorized"] == 1
        await db_session.refresh(waiting)
        await db_session.refresh(confirmed)
        assert waiting.category_id == new
        assert confirmed.category_id == old

    async def test_a_moved_row_stays_in_the_queue(self, api_client, db_session):
        # It took the category, but nobody has looked at it: a low-confidence
        # row can still have the wrong amount, and marking it reviewed would
        # say otherwise.
        household, account = await a_household(api_client, "+14165574011")
        _, new = await two_system_categories(db_session)
        corrected = at(household, account, day=3, merchant="Spotify")
        waiting = at(household, account, day=4, merchant="Spotify")
        db_session.add_all([corrected, waiting])
        await db_session.flush()

        await patch(api_client, corrected, category_id=str(new))

        await db_session.refresh(waiting)
        assert waiting.needs_review is True

    async def test_other_merchants_are_untouched(self, api_client, db_session):
        household, account = await a_household(api_client, "+14165574012")
        _, new = await two_system_categories(db_session)
        corrected = at(household, account, day=3, merchant="Spotify")
        other = at(household, account, day=4, merchant="Netflix.com")
        db_session.add_all([corrected, other])
        await db_session.flush()

        response = await patch(api_client, corrected, category_id=str(new))

        assert response.json()["recategorized"] == 0
        await db_session.refresh(other)
        assert other.category_id is None


class TestNeverAcrossHouseholds:
    async def test_one_household_s_rule_does_not_move_another_s_rows(
        self, api_client, db_session
    ):
        theirs, their_account = await a_household(api_client, "+14165574020")
        _, new = await two_system_categories(db_session)
        their_row = at(theirs, their_account, day=4, merchant="Spotify")
        db_session.add(their_row)
        await db_session.flush()

        mine, my_account = await a_household(api_client, "+14165574021")
        my_row = at(mine, my_account, day=3, merchant="Spotify")
        db_session.add(my_row)
        await db_session.flush()

        response = await patch(api_client, my_row, category_id=str(new))

        assert response.json()["recategorized"] == 0
        await db_session.refresh(their_row)
        assert their_row.category_id is None

    async def test_one_household_s_rule_never_reaches_another_s_prompt(
        self, api_client, db_session
    ):
        _, new = await two_system_categories(db_session)
        mine, my_account = await a_household(api_client, "+14165574022")
        row = at(mine, my_account, day=3, merchant="Private Clinic")
        db_session.add(row)
        await db_session.flush()
        await patch(api_client, row, category_id=str(new))

        theirs, _ = await a_household(api_client, "+14165574023")

        assert "private clinic" in await _examples(db_session, mine)
        assert "private clinic" not in await _examples(db_session, theirs)

    async def test_the_rule_count_is_per_household(self, api_client, db_session):
        # The same merchant corrected by two households is two rules, one each.
        # A global uniqueness on the pattern would have made the second
        # household's correction overwrite the first's.
        _, new = await two_system_categories(db_session)
        households = []
        for phone in ("+14165574024", "+14165574025"):
            household, account = await a_household(api_client, phone)
            row = at(household, account, day=3, merchant="Spotify")
            db_session.add(row)
            await db_session.flush()
            await patch(api_client, row, category_id=str(new))
            households.append(household)

        for household in households:
            assert [r.merchant_pattern for r in await rules(db_session, household)] == [
                "spotify"
            ]


class TestTheRuleShowsUpNext:
    async def test_a_re_corrected_rule_moves_to_the_front_of_the_prompt(
        self, api_client, db_session
    ):
        # The prompt shows the most recently updated rules and keeps only a
        # handful. An upsert does not fire `onupdate`, so without setting
        # updated_at by hand a re-corrected rule would stay wherever it was
        # first written — possibly outside the handful the model sees.
        household, account = await a_household(api_client, "+14165574030")
        first, second = await two_system_categories(db_session)
        spotify_one = at(household, account, day=3, merchant="Spotify")
        netflix = at(household, account, day=4, merchant="Netflix.com")
        spotify_two = at(household, account, day=5, merchant="Spotify")
        db_session.add_all([spotify_one, netflix, spotify_two])
        await db_session.flush()

        await patch(api_client, spotify_one, category_id=str(first))
        await patch(api_client, netflix, category_id=str(first))
        await patch(api_client, spotify_two, category_id=str(second))

        prompt = await _examples(db_session, household)
        assert prompt.index('"spotify"') < prompt.index('"netflix.com"')
