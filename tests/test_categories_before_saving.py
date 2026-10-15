"""Categories while checking an import, before anything is saved (#55 follow-up).

The statement is categorised when it is read, so the app can show and let the
person change every category before saving. On save the server keeps what the
person left; a category they changed teaches a rule, as fixing a saved row
does; a row left without one goes to review, not back to the model.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.api import statements as endpoint
from app.models.categorization import Category, CategoryCorrection
from app.models.enums import ReviewReason
from app.models.money import Transaction
from app.services import filing
from tests.conftest import requires_db
from tests.test_statements_endpoint import (
    GOOD_ANSWER,
    PARSE,
    body,
    consent_to_ai,
    onboard,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]


class Model:
    """Reads the statement, and categorises: TIM HORTONS is dining, and
    anything else is a slug the taxonomy does not have."""

    def __init__(self) -> None:
        self.categorised: list[list] = []

    @property
    def model(self) -> str:
        return "fake-1"

    async def complete(self, *, system: str, user: str, max_output_tokens: int) -> str:
        if system.startswith("You put personal transactions into categories"):
            pairs = json.loads(user)
            self.categorised.append(pairs)
            return json.dumps(
                [
                    "dining" if "TIM HORTONS" in m.upper() else "not_a_slug"
                    for m, _ in pairs
                ]
            )
        return GOOD_ANSWER


@pytest.fixture
def model(monkeypatch) -> Model:
    fake = Model()
    monkeypatch.setattr(endpoint, "build_client", lambda _s: fake)
    monkeypatch.setattr(filing, "build_client", lambda _s: fake)
    return fake


async def system_id(db_session, slug: str) -> uuid.UUID:
    return await db_session.scalar(
        select(Category.id).where(
            Category.household_id.is_(None), Category.slug == slug
        )
    )


async def ready(api_client, phone: str) -> tuple[str, str]:
    """Onboarded, consented, with a chequing account: (household id, account id)."""
    await onboard(api_client, phone)
    await consent_to_ai(api_client)
    household = (await api_client.get("/me")).json()["household"]["id"]
    account = await api_client.post(
        "/accounts", json={"name": "RBC Chequing", "kind": "chequing"}
    )
    return household, account.json()["id"]


async def save(api_client, parsed: dict, account: str, rows: list[dict]):
    return await api_client.post(
        f"/statements/{parsed['import_id']}/transactions",
        json={"account_id": account, "rows": rows, "categorised": True},
    )


def as_sent(row: dict, category_id=None) -> dict:
    """A parsed row as the app sends it back: its suggestion echoed."""
    return {
        **{
            k: row[k]
            for k in ("occurred_on", "description", "amount", "direction", "confidence")
        },
        "category_id": category_id if category_id is not None else row["category_id"],
        "suggested_category_id": row["category_id"],
    }


async def saved(db_session, household: str, description: str) -> Transaction:
    found = (
        await db_session.execute(
            select(Transaction).where(
                Transaction.household_id == uuid.UUID(household),
                Transaction.description == description,
            )
        )
    ).scalar_one()
    await db_session.refresh(found)
    return found


class TestReading:
    async def test_each_row_comes_back_with_a_suggested_category(
        self, api_client, db_session, model
    ):
        await ready(api_client, "+14165579001")

        parsed = (await api_client.post(PARSE, json=body())).json()

        dining = await system_id(db_session, "dining")
        by_name = {r["description"]: r["category_id"] for r in parsed["rows"]}
        assert by_name == {
            "TIM HORTONS": str(dining),
            "LOBLAWS": None,
        }, "an answer outside the taxonomy is no suggestion, not a guess"
        # Only merchant and amount reach the model, as on save.
        assert all(len(pair) == 2 for pair in model.categorised[0])

    async def test_a_households_rule_wins_without_asking_the_model(
        self, api_client, db_session, model
    ):
        household, _ = await ready(api_client, "+14165579002")
        groceries = await system_id(db_session, "groceries")
        db_session.add(
            CategoryCorrection(
                household_id=uuid.UUID(household),
                merchant_pattern="loblaws",
                corrected_category_id=groceries,
            )
        )
        await db_session.flush()

        parsed = (await api_client.post(PARSE, json=body())).json()

        by_name = {r["description"]: r["category_id"] for r in parsed["rows"]}
        assert by_name["LOBLAWS"] == str(groceries)
        assert [m.upper() for m, _ in model.categorised[0]] == [
            "TIM HORTONS"
        ], "the ruled merchant is never sent"


class TestSaving:
    async def test_categories_are_kept_and_the_model_is_not_asked_again(
        self, api_client, db_session, model
    ):
        household, account = await ready(api_client, "+14165579010")
        parsed = (await api_client.post(PARSE, json=body())).json()
        asked = len(model.categorised)

        response = await save(
            api_client, parsed, account, [as_sent(r) for r in parsed["rows"]]
        )

        assert response.status_code == 200, response.text
        assert len(model.categorised) == asked, "categorised once, when read"
        tim = await saved(db_session, household, "TIM HORTONS")
        loblaws = await saved(db_session, household, "LOBLAWS")
        assert tim.category_id == await system_id(db_session, "dining")
        assert tim.needs_review is False
        assert loblaws.category_id is None
        assert (
            loblaws.review_reason == ReviewReason.unknown_category
        ), "left without a category: to review, not back to the model"

    async def test_a_category_the_person_changed_teaches_a_rule(
        self, api_client, db_session, model
    ):
        household, account = await ready(api_client, "+14165579011")
        parsed = (await api_client.post(PARSE, json=body())).json()
        groceries = await system_id(db_session, "groceries")
        rows = [
            as_sent(r, str(groceries)) if r["description"] == "LOBLAWS" else as_sent(r)
            for r in parsed["rows"]
        ]

        response = await save(api_client, parsed, account, rows)

        assert response.status_code == 200, response.text
        assert (await saved(db_session, household, "LOBLAWS")).category_id == groceries
        rules = (
            (
                await db_session.execute(
                    select(CategoryCorrection.merchant_pattern).where(
                        CategoryCorrection.household_id == uuid.UUID(household)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert list(rules) == ["loblaws"], "the accepted TIM HORTONS teaches nothing"

    async def test_a_category_it_cannot_use_is_refused(
        self, api_client, db_session, model
    ):
        """Only a shared category or the household's own: never one it cannot
        see, and nothing is saved when one row names such a category."""
        household, account = await ready(api_client, "+14165579012")
        parsed = (await api_client.post(PARSE, json=body())).json()
        rows = [as_sent(r, str(uuid.uuid4())) for r in parsed["rows"]]

        response = await save(api_client, parsed, account, rows)

        assert response.status_code == 422
        assert response.json()["detail"]["field"] == "rows.0.category_id"
        anything = await db_session.scalar(
            select(Transaction.id).where(
                Transaction.household_id == uuid.UUID(household)
            )
        )
        assert anything is None, "nothing saved"

    async def test_an_app_that_sends_no_categories_is_categorised_on_save(
        self, api_client, db_session, model
    ):
        """Released builds before this change: every row categorised on save,
        as before."""
        household, account = await ready(api_client, "+14165579013")
        parsed = (await api_client.post(PARSE, json=body())).json()
        plain = [
            {
                k: r[k]
                for k in (
                    "occurred_on",
                    "description",
                    "amount",
                    "direction",
                    "confidence",
                )
            }
            for r in parsed["rows"]
        ]

        response = await api_client.post(
            f"/statements/{parsed['import_id']}/transactions",
            json={"account_id": account, "rows": plain},
        )

        assert response.status_code == 200, response.text
        tim = await saved(db_session, household, "TIM HORTONS")
        assert tim.category_id == await system_id(db_session, "dining")
