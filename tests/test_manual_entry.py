"""A transaction typed in by hand (#38).

Since the vision fallback was removed, typing it in is the only way in when a
document cannot be read — so these hold the typed row to the same standard as
an imported one, against a real Postgres, counting rows.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import structlog
from sqlalchemy import func, select

from app.models.categorization import Category, CategoryCorrection
from app.models.enums import TransactionDirection, TransactionSource
from app.models.money import Transaction
from app.services.normalization import normalized
from tests.conftest import requires_db
from tests.test_ledger_endpoint import FakeModel, authenticate_as, use_model

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

TODAY = datetime.now(UTC).date()
DAY = (TODAY - timedelta(days=3)).isoformat()


async def a_household(api_client, phone: str, **account) -> tuple[str, str]:
    """Onboard a caller past the terms, with one account. No AI consent."""
    authenticate_as(phone=phone)
    me = (await api_client.get("/me")).json()
    version = (await api_client.get("/legal/terms")).json()["version"]
    await api_client.post("/me/consent", json={"version": version})
    response = await api_client.post(
        "/accounts", json={"name": "RBC Chequing", "kind": "chequing", **account}
    )
    assert response.status_code == 201, response.text
    return me["household"]["id"], response.json()["id"]


async def consent_to_ai(api_client) -> None:
    version = (await api_client.get("/legal/ai-processing")).json()["version"]
    response = await api_client.post(
        "/legal/ai-processing/consent", json={"version": version}
    )
    assert response.status_code == 200, response.text


def entry(account_id: str, **overrides) -> dict:
    return {
        "account_id": account_id,
        "occurred_on": DAY,
        "amount": "5.25",
        "direction": "debit",
        "description": "Tim Hortons",
        **overrides,
    }


async def add(api_client, body: dict):
    return await api_client.post("/transactions", json=body)


async def count(db_session, account_id: str) -> int:
    result = await db_session.execute(
        select(func.count())
        .select_from(Transaction)
        .where(Transaction.account_id == uuid.UUID(account_id))
    )
    return result.scalar_one()


async def stored(db_session, transaction_id: str) -> Transaction:
    row = await db_session.get(Transaction, uuid.UUID(transaction_id))
    await db_session.refresh(row)
    return row


class TestSaving:
    async def test_it_is_the_row_an_import_would_write_marked_manual(
        self, api_client, db_session, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        _, account = await a_household(api_client, "+14165590001")
        description = "TIM HORTONS #4821 MISSISSAUGA"

        response = await add(
            api_client, entry(account, description=description, amount="1200.50")
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["source"] == "manual"
        row = await stored(db_session, body["id"])
        assert row.source is TransactionSource.manual
        assert row.statement_import_id is None
        # The same function the import path keys on — so a later import of
        # the same purchase meets this row in the dedup index.
        assert row.normalized_description == normalized(description)
        assert row.occurrence == 1
        assert row.direction is TransactionDirection.debit

    @pytest.mark.parametrize(
        ("typed", "stored_minor", "sent_back"),
        [("1200.5", 120050, "1200.50"), ("0.01", 1, "0.01"), ("1,234.56", None, None)],
    )
    async def test_the_amount_round_trips_as_a_decimal_string(
        self, api_client, db_session, monkeypatch, typed, stored_minor, sent_back
    ):
        use_model(monkeypatch, FakeModel())
        _, account = await a_household(api_client, "+14165590002")

        response = await add(api_client, entry(account, amount=typed))

        if stored_minor is None:
            # Grouping separators are the client's to strip (mobile #30 sends
            # the normalized form); the money boundary does not guess.
            assert response.status_code == 422
            assert response.json()["detail"]["field"] == "amount"
            return
        assert response.status_code == 201, response.text
        assert response.json()["amount"] == sent_back
        row = await stored(db_session, response.json()["id"])
        assert row.amount_minor_units == stored_minor

    async def test_a_zero_decimal_currency_keeps_its_scale(
        self, api_client, db_session, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        _, account = await a_household(api_client, "+14165590003", currency="JPY")

        whole = await add(api_client, entry(account, amount="1200"))
        fraction = await add(
            api_client, entry(account, amount="1200.50", description="Lawson")
        )

        assert whole.status_code == 201, whole.text
        assert (whole.json()["amount"], whole.json()["currency"]) == ("1200", "JPY")
        # Excess precision raises rather than rounding a yen away.
        assert fraction.status_code == 422
        assert fraction.json()["detail"]["field"] == "amount"


class TestDuplicates:
    async def test_an_exact_copy_is_refused_naming_the_match(
        self, api_client, db_session, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        household, account = await a_household(api_client, "+14165590010")
        first = (await add(api_client, entry(account))).json()

        with structlog.testing.capture_logs() as logs:
            again = await add(api_client, entry(account))

        assert again.status_code == 409
        # The exact shape mobile #30 reads: under `detail`, the match in full.
        detail = again.json()["detail"]
        assert detail["code"] == "duplicate_transaction"
        assert detail["duplicate_of"] == {
            "id": first["id"],
            "occurred_on": DAY,
            "amount": "5.25",
            "description": "Tim Hortons",
        }
        assert await count(db_session, account) == 1
        (line,) = [e for e in logs if e["event"] == "conflict"]
        assert line["code"] == "duplicate_transaction"

    async def test_the_same_purchase_typed_differently_is_still_the_same(
        self, api_client, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        _, account = await a_household(api_client, "+14165590011")
        await add(api_client, entry(account, description="TIM HORTONS #4821"))

        again = await add(api_client, entry(account, description="  tim hortons  "))

        assert again.status_code == 409

    async def test_keeping_both_takes_the_next_occurrence_each_time(
        self, api_client, db_session, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        _, account = await a_household(api_client, "+14165590012")
        first = await add(api_client, entry(account))
        second = await add(api_client, entry(account, allow_duplicate=True))
        third = await add(api_client, entry(account, allow_duplicate=True))

        assert [r.status_code for r in (first, second, third)] == [201, 201, 201]
        occurrences = [
            (await stored(db_session, r.json()["id"])).occurrence
            for r in (first, second, third)
        ]
        assert occurrences == [1, 2, 3]

    async def test_an_imported_row_is_matched_too(
        self, api_client, db_session, monkeypatch
    ):
        # The case the ticket names: typing in a coffee already imported.
        use_model(monkeypatch, FakeModel())
        household, account = await a_household(api_client, "+14165590013")
        imported = Transaction(
            household_id=uuid.UUID(household),
            account_id=uuid.UUID(account),
            occurred_on=TODAY - timedelta(days=3),
            amount_minor_units=525,
            currency="CAD",
            direction=TransactionDirection.debit,
            description="TIM HORTONS #4821",
            normalized_description=normalized("TIM HORTONS #4821"),
            source=TransactionSource.upload,
        )
        db_session.add(imported)
        await db_session.flush()

        response = await add(api_client, entry(account))

        assert response.status_code == 409
        assert response.json()["detail"]["duplicate_of"]["id"] == str(imported.id)

    async def test_allow_duplicate_with_nothing_to_duplicate_is_just_a_save(
        self, api_client, db_session, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        _, account = await a_household(api_client, "+14165590014")

        response = await add(api_client, entry(account, allow_duplicate=True))

        assert response.status_code == 201
        assert (await stored(db_session, response.json()["id"])).occurrence == 1

    async def test_same_day_and_amount_with_another_name_is_flagged_not_refused(
        self, api_client, db_session, monkeypatch
    ):
        # Manager decision (2026-09-29): the import rule, applied to typed rows.
        use_model(monkeypatch, FakeModel())
        _, account = await a_household(api_client, "+14165590015")
        first = (await add(api_client, entry(account))).json()
        # A chosen category, so nothing else can send this row to review: the
        # flag below is the near match's doing alone.
        dining = (
            await db_session.execute(
                select(Category.id).where(
                    Category.household_id.is_(None), Category.slug == "dining"
                )
            )
        ).scalar_one()

        response = await add(
            api_client,
            entry(account, description="Starbucks", category_id=str(dining)),
        )

        assert response.status_code == 201
        assert response.json()["needs_review"] is True
        assert response.json()["review_reason"] == "suspected_duplicate"
        row = await stored(db_session, response.json()["id"])
        assert row.duplicate_of_id == uuid.UUID(first["id"])
        assert await count(db_session, account) == 2


class TestCategories:
    async def test_a_chosen_category_is_kept_and_the_model_never_asked(
        self, api_client, db_session, monkeypatch
    ):
        model = FakeModel(answer='["dining"]')
        use_model(monkeypatch, model)
        _, account = await a_household(api_client, "+14165590020")
        await consent_to_ai(api_client)
        groceries = (
            await db_session.execute(
                select(Category.id).where(
                    Category.household_id.is_(None), Category.slug == "groceries"
                )
            )
        ).scalar_one()

        response = await add(api_client, entry(account, category_id=str(groceries)))

        assert response.status_code == 201, response.text
        assert response.json()["category_id"] == str(groceries)
        assert model.prompts == []

    async def test_a_household_rule_files_it_without_the_model(
        self, api_client, db_session, monkeypatch
    ):
        model = FakeModel()
        use_model(monkeypatch, model)
        household, account = await a_household(api_client, "+14165590021")
        own = Category(
            household_id=uuid.UUID(household), slug="coffee_habit", name="Coffee"
        )
        db_session.add(own)
        await db_session.flush()
        db_session.add(
            CategoryCorrection(
                household_id=uuid.UUID(household),
                merchant_pattern="tim hortons",
                corrected_category_id=own.id,
            )
        )
        await db_session.flush()

        response = await add(api_client, entry(account))

        assert response.json()["category_id"] == str(own.id)
        assert response.json()["needs_review"] is False
        assert model.prompts == []

    async def test_with_consent_the_model_gets_only_merchant_and_amount(
        self, api_client, monkeypatch
    ):
        model = FakeModel(answer='["dining"]')
        use_model(monkeypatch, model)
        _, account = await a_household(api_client, "+14165590022")
        await consent_to_ai(api_client)

        response = await add(
            api_client, entry(account, description="TIM HORTONS #4821 MISSISSAUGA")
        )

        assert response.status_code == 201
        assert response.json()["category_id"] is not None
        assert response.json()["needs_review"] is False
        # PRD Appendix A.3: a shop name and a price, and nothing else.
        assert json.loads(model.prompts[0]) == [["Tim Hortons Mississauga", "5.25"]]

    async def test_without_consent_the_model_is_never_asked(
        self, api_client, monkeypatch
    ):
        # Manager decision (2026-09-29): rules, then a person — never a
        # provider the person did not agree to.
        model = FakeModel(answer='["dining"]')
        use_model(monkeypatch, model)
        _, account = await a_household(api_client, "+14165590023")

        response = await add(api_client, entry(account))

        assert response.status_code == 201
        assert model.prompts == []
        assert response.json()["category_id"] is None
        assert response.json()["needs_review"] is True
        assert response.json()["review_reason"] == "unknown_category"

    async def test_production_without_the_no_training_tier_never_asks_the_model(
        self, api_client, monkeypatch
    ):
        # The consent text says the provider may not train on this data; until
        # someone confirms that tier, production must not send it — the same
        # line the parse endpoint holds.
        from app.config import Settings, get_settings

        model = FakeModel(answer='["dining"]')
        use_model(monkeypatch, model)
        _, account = await a_household(api_client, "+14165590024")
        await consent_to_ai(api_client)
        production = Settings(
            **{**get_settings().model_dump(), "app_env": "production"}
        )
        monkeypatch.setattr("app.api.transactions.get_settings", lambda: production)

        response = await add(api_client, entry(account))

        assert response.status_code == 201
        assert model.prompts == []
        assert response.json()["review_reason"] == "unknown_category"

    async def test_another_household_s_category_is_refused(
        self, api_client, db_session, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        theirs, _ = await a_household(api_client, "+14165590025")
        private = Category(household_id=uuid.UUID(theirs), slug="secret", name="S")
        db_session.add(private)
        await db_session.flush()
        _, account = await a_household(api_client, "+14165590026")

        response = await add(api_client, entry(account, category_id=str(private.id)))

        assert response.status_code == 422
        assert response.json()["detail"]["field"] == "category_id"
        assert await count(db_session, account) == 0


class TestRefusals:
    @pytest.mark.parametrize(
        ("overrides", "field"),
        [
            ({"occurred_on": (TODAY + timedelta(days=5)).isoformat()}, "occurred_on"),
            ({"amount": "-5.25"}, "amount"),
            ({"amount": "abc"}, "amount"),
            ({"description": ""}, "description"),
            ({"description": "   "}, "description"),
            # Manager decision (2026-09-29): a description with no name in it
            # normalizes to an empty key, which can be neither categorized nor
            # told apart from any other such entry.
            ({"description": "12345"}, "description"),
            ({"description": "#88 04/05"}, "description"),
            ({"description": "x" * 513}, "description"),
            ({"direction": "sideways"}, "direction"),
            ({"source": "upload"}, "source"),
        ],
    )
    async def test_a_bad_field_is_a_422_naming_it(
        self, api_client, db_session, monkeypatch, overrides, field
    ):
        use_model(monkeypatch, FakeModel())
        _, account = await a_household(api_client, "+14165590030")

        response = await add(api_client, entry(account, **overrides))

        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        named = (
            detail["field"]
            if isinstance(detail, dict)
            else [error["loc"][-1] for error in detail]
        )
        assert field in named
        assert await count(db_session, account) == 0

    async def test_a_day_ahead_is_tolerated_for_timezones(
        self, api_client, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        _, account = await a_household(api_client, "+14165590031")

        response = await add(
            api_client,
            entry(account, occurred_on=(TODAY + timedelta(days=1)).isoformat()),
        )

        assert response.status_code == 201, response.text

    async def test_a_missing_account_and_someone_else_s_answer_alike(
        self, api_client, db_session, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        _, theirs = await a_household(api_client, "+14165590032")
        await a_household(api_client, "+14165590033")

        missing = await add(api_client, entry(str(uuid.uuid4())))
        not_mine = await add(api_client, entry(theirs))

        for response in (missing, not_mine):
            assert response.status_code == 404
            assert response.json()["detail"]["code"] == "unknown_account"
            assert response.json()["detail"]["field"] == "account_id"
        # Identical, so the answer says nothing about which ids exist.
        assert missing.json() == not_mine.json()
        assert await count(db_session, theirs) == 0


class TestEditing:
    """No `PUT`: 3.4's `PATCH` already edits any row the household owns, and
    these hold it to that for a row with no import behind it."""

    async def test_patch_corrects_a_manual_row(
        self, api_client, db_session, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        _, account = await a_household(api_client, "+14165590040")
        made = (await add(api_client, entry(account))).json()

        response = await api_client.patch(
            f"/transactions/{made['id']}",
            json={"amount": "6.25", "description": "Tim Hortons Oakville"},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["transaction"]["amount"] == "6.25"
        assert body["transaction"]["source"] == "manual"
        # No import to finish: the flag must stay false, not error.
        assert body["import_finished"] is False
        row = await stored(db_session, made["id"])
        assert row.normalized_description == normalized("Tim Hortons Oakville")

    async def test_patch_into_a_copy_of_another_manual_row_is_refused(
        self, api_client, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        _, account = await a_household(api_client, "+14165590041")
        await add(api_client, entry(account))
        other = (await add(api_client, entry(account, amount="6.25"))).json()

        response = await api_client.patch(
            f"/transactions/{other['id']}", json={"amount": "5.25"}
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "would_duplicate"

    async def test_a_manual_row_answers_the_review_queue_like_any_other(
        self, api_client, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        _, account = await a_household(api_client, "+14165590042")
        made = (await add(api_client, entry(account))).json()
        assert made["review_reason"] == "unknown_category"

        queue = (await api_client.get("/transactions/review")).json()["rows"]

        assert made["id"] in {row["id"] for row in queue}
