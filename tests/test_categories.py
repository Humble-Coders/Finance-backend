"""Categories a household makes for itself.

The promise that matters: no request, however shaped, creates a system
category — one with no household, seen by everybody.
"""

from __future__ import annotations

import uuid

import pytest
import structlog
from sqlalchemy import func, select

from app.models.categorization import Category
from app.services.categories import SLUG_MAX, slug_for
from tests.conftest import requires_db
from tests.test_transactions_review import a_household, flagged, patch


class TestSlugs:
    @pytest.mark.parametrize(
        ("name", "slug"),
        [
            ("Groceries", "groceries"),
            ("Kids' Activities", "kids_activities"),
            ("  Side   Business  ", "side_business"),
            # Folded, not dropped: `cafe`, never `caf`.
            ("Café", "cafe"),
            ("Pets & Vet", "pets_vet"),
        ],
    )
    def test_a_name_becomes_its_slug(self, name, slug):
        assert slug_for(name) == slug

    def test_slugs_are_spelled_like_the_taxonomy(self):
        # The seeded taxonomy uses underscores (`debt_payment`). A hyphenated
        # slug would miss the clash with it and put two "Debt payment" choices
        # in the picker.
        assert slug_for("Debt payment") == "debt_payment"

    @pytest.mark.parametrize("name", ["🎉🎉", "!!!", "   ", "—"])
    def test_a_name_with_no_letters_has_no_slug(self, name):
        assert slug_for(name) is None

    def test_a_long_name_is_cut_at_a_word_not_through_one(self):
        slug = slug_for("word " * 40)

        assert slug is not None
        assert len(slug) <= SLUG_MAX
        assert not slug.endswith("_")
        assert all(part == "word" for part in slug.split("_"))


db = [pytest.mark.asyncio(loop_scope="session"), requires_db]


async def system_count(db_session) -> int:
    result = await db_session.execute(
        select(func.count())
        .select_from(Category)
        .where(Category.household_id.is_(None))
    )
    return result.scalar_one()


class TestCreating:
    pytestmark = db

    async def test_a_category_belongs_to_the_household_that_made_it(
        self, api_client, db_session
    ):
        household, _ = await a_household(api_client, "+14165577001")

        response = await api_client.post("/categories", json={"name": "Side business"})

        assert response.status_code == 201, response.text
        body = response.json()
        assert (body["slug"], body["is_system"]) == ("side_business", False)
        made = await db_session.get(Category, uuid.UUID(body["id"]))
        assert made.household_id == household

    async def test_another_household_may_use_the_same_name(
        self, api_client, db_session
    ):
        # Each household's names are its own. Refusing this would also tell the
        # second household that somebody else had used the name.
        await a_household(api_client, "+14165577002")
        await api_client.post("/categories", json={"name": "Side business"})

        await a_household(api_client, "+14165577003")
        response = await api_client.post("/categories", json={"name": "Side business"})

        assert response.status_code == 201, response.text

    async def test_a_name_with_no_letters_is_refused(self, api_client):
        await a_household(api_client, "+14165577004")

        response = await api_client.post("/categories", json={"name": "🎉🎉"})

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "unnamed_category"

    async def test_it_can_be_filed_into_straight_away(self, api_client, db_session):
        # The reason this endpoint exists: a correction pointing at a category
        # the taxonomy does not have.
        household, account = await a_household(api_client, "+14165577005")
        made = (
            await api_client.post("/categories", json={"name": "Side business"})
        ).json()
        row = flagged(household, account, day=4)
        db_session.add(row)
        await db_session.flush()

        response = await patch(api_client, row, category_id=made["id"])

        assert response.status_code == 200, response.text
        assert response.json()["transaction"]["category_id"] == made["id"]


class TestNeverASystemCategory:
    pytestmark = db

    @pytest.mark.parametrize(
        "extra",
        [
            {"household_id": None},
            {"household_id": str(uuid.uuid4())},
            {"is_system": True},
            {"slug": "groceries"},
            {"parent_id": str(uuid.uuid4())},
        ],
    )
    async def test_anything_but_a_name_is_refused(self, api_client, db_session, extra):
        await a_household(api_client, "+14165577010")
        before = await system_count(db_session)

        response = await api_client.post(
            "/categories", json={"name": "Totally new thing", **extra}
        )

        assert response.status_code == 422
        assert await system_count(db_session) == before

    async def test_a_plain_request_never_makes_a_system_row(
        self, api_client, db_session
    ):
        await a_household(api_client, "+14165577011")
        before = await system_count(db_session)

        response = await api_client.post("/categories", json={"name": "Hobbies"})

        assert response.status_code == 201
        assert await system_count(db_session) == before


class TestAlreadyThere:
    pytestmark = db

    async def test_the_same_name_twice_is_a_logged_409(self, api_client):
        await a_household(api_client, "+14165577020")
        first = (await api_client.post("/categories", json={"name": "Hobbies"})).json()

        with structlog.testing.capture_logs() as logs:
            again = await api_client.post("/categories", json={"name": "Hobbies"})

        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "category_exists"
        # Names the one that exists, so the client can use it instead.
        assert again.json()["detail"]["category_id"] == first["id"]
        (line,) = [entry for entry in logs if entry["event"] == "conflict"]
        assert line["code"] == "category_exists"

    async def test_case_and_spacing_are_the_same_name(self, api_client):
        await a_household(api_client, "+14165577021")
        await api_client.post("/categories", json={"name": "Side business"})

        again = await api_client.post("/categories", json={"name": "SIDE   Business"})

        assert again.status_code == 409

    async def test_a_system_category_s_name_is_refused_and_named(
        self, api_client, db_session
    ):
        await a_household(api_client, "+14165577022")
        system = (
            await db_session.execute(
                select(Category).where(
                    Category.household_id.is_(None), Category.slug == "dining"
                )
            )
        ).scalar_one()

        response = await api_client.post("/categories", json={"name": "Dining"})

        assert response.status_code == 409
        assert response.json()["detail"]["category_id"] == str(system.id)

    async def test_another_household_s_category_does_not_block_or_leak(
        self, api_client, db_session
    ):
        theirs, _ = await a_household(api_client, "+14165577023")
        private = Category(household_id=theirs, slug="secret_project", name="Secret")
        db_session.add(private)
        await db_session.flush()

        await a_household(api_client, "+14165577024")
        response = await api_client.post("/categories", json={"name": "Secret project"})

        assert response.status_code == 201, response.text
        assert response.json()["id"] != str(private.id)


class TestListing:
    pytestmark = db

    async def test_the_shared_categories_are_all_listed(self, api_client, db_session):
        await a_household(api_client, "+14165577030")

        listed = (await api_client.get("/categories")).json()

        assert sum(c["is_system"] for c in listed) == await system_count(db_session)

    async def test_the_household_s_own_are_listed_with_them(self, api_client):
        await a_household(api_client, "+14165577031")
        made = (await api_client.post("/categories", json={"name": "Hobbies"})).json()

        listed = (await api_client.get("/categories")).json()

        mine = [c for c in listed if c["id"] == made["id"]]
        assert mine == [
            {"id": made["id"], "slug": "hobbies", "name": "Hobbies", "is_system": False}
        ]

    async def test_another_household_s_are_never_listed(self, api_client, db_session):
        # Their category names would say what somebody else spends on.
        theirs, _ = await a_household(api_client, "+14165577032")
        private = Category(household_id=theirs, slug="fertility_clinic", name="Clinic")
        db_session.add(private)
        await db_session.flush()

        await a_household(api_client, "+14165577033")
        listed = (await api_client.get("/categories")).json()

        assert str(private.id) not in {c["id"] for c in listed}
        assert not any(c["slug"] == "fertility_clinic" for c in listed)

    async def test_they_come_back_in_name_order(self, api_client):
        await a_household(api_client, "+14165577034")
        await api_client.post("/categories", json={"name": "aardvark care"})

        names = [c["name"] for c in (await api_client.get("/categories")).json()]

        assert names == sorted(names, key=str.lower)
        assert names[0] == "aardvark care"

    async def test_every_listed_id_can_be_filed_into(self, api_client, db_session):
        # The point of listing them: whatever the picker offers, PATCH accepts.
        household, account = await a_household(api_client, "+14165577035")
        await api_client.post("/categories", json={"name": "Hobbies"})
        listed = (await api_client.get("/categories")).json()

        # A distinct amount per row: identical rows on one account and day
        # would collide on the dedup index before the category was ever tried.
        for i, category in enumerate(listed):
            row = flagged(household, account, day=4, minor=100 + i)
            db_session.add(row)
            await db_session.flush()
            response = await patch(api_client, row, category_id=category["id"])
            assert response.status_code == 200, (category["slug"], response.text)
