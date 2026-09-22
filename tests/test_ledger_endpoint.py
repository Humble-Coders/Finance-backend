"""Saving an import: accounts, dedup, and what reaches the model.

Every bug this ticket guards against is invisible to a passing unit test — a
double-counted statement raises nothing, returns 200, and quietly tells someone
they spent money they did not. So these run against a real Postgres and count
rows.

Imports are created directly rather than through `/statements/parse`, because
the free tier allows one parse a month and several of these need two imports.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.api import statements as endpoint
from app.auth import AuthenticatedUser, current_user
from app.models.categorization import Category, CategoryCorrection
from app.models.enums import ReviewReason, SourceKind, StatementImportStatus
from app.models.money import StatementImport, Transaction
from app.services.normalization import normalized
from tests.conftest import requires_db

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]


class FakeModel:
    """Records what it was asked, so a test can assert what left the building."""

    def __init__(self, answer: str | None = None) -> None:
        self.answer = answer
        self.prompts: list[str] = []
        self.systems: list[str] = []

    @property
    def model(self) -> str:
        return "fake-1"

    async def complete(self, *, system: str, user: str, max_output_tokens: int) -> str:
        self.systems.append(system)
        self.prompts.append(user)
        if self.answer is not None:
            return self.answer
        return json.dumps(["other"] * len(json.loads(user)))


def authenticate_as(*, phone: str) -> None:
    from app.main import app

    app.dependency_overrides[current_user] = lambda: AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        email=None,
        phone=phone,
        claims={"app_metadata": {"provider": "phone"}},
    )


def use_model(monkeypatch, model) -> None:
    monkeypatch.setattr(endpoint, "build_client", lambda _s: model)


async def onboard(api_client, phone: str) -> dict:
    authenticate_as(phone=phone)
    # Resolves the caller to a household, creating it on first call.
    await api_client.get("/me")
    version = (await api_client.get("/legal/terms")).json()["version"]
    return (await api_client.post("/me/consent", json={"version": version})).json()


async def an_account(api_client, name: str = "RBC Chequing") -> str:
    response = await api_client.post(
        "/accounts", json={"name": name, "kind": "chequing"}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def an_import(db_session, household_id: str) -> str:
    record = StatementImport(
        household_id=uuid.UUID(household_id),
        source_kind=SourceKind.pdf_text,
        status=StatementImportStatus.awaiting_review,
    )
    db_session.add(record)
    await db_session.flush()
    return str(record.id)


def row(
    amount="5.25", description="TIM HORTONS #4821", day="2026-08-02", confidence=95
):
    return {
        "occurred_on": day,
        "description": description,
        "amount": amount,
        "direction": "debit",
        "confidence": confidence,
    }


async def save(api_client, import_id, account_id, rows):
    return await api_client.post(
        f"/statements/{import_id}/transactions",
        json={"account_id": account_id, "rows": rows},
    )


class TestAccounts:
    async def test_create_and_list(self, api_client):
        await onboard(api_client, "+14165572001")
        await an_account(api_client, "RBC Chequing")
        await an_account(api_client, "Visa")

        listed = (await api_client.get("/accounts")).json()

        assert [a["name"] for a in listed] == ["RBC Chequing", "Visa"]

    async def test_a_duplicate_name_is_refused(self, api_client):
        """Two accounts for one real account split a household's statements
        into piles that cannot see each other's duplicates."""
        await onboard(api_client, "+14165572002")
        await an_account(api_client, "RBC Chequing")

        again = await api_client.post(
            "/accounts", json={"name": "RBC Chequing", "kind": "chequing"}
        )

        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "duplicate_account_name"

    async def test_two_households_may_each_have_one(self, api_client):
        await onboard(api_client, "+14165572003")
        await an_account(api_client, "RBC Chequing")

        await onboard(api_client, "+14165572004")
        second = await api_client.post(
            "/accounts", json={"name": "RBC Chequing", "kind": "chequing"}
        )

        assert second.status_code == 201

    async def test_one_household_never_sees_another_s(self, api_client):
        await onboard(api_client, "+14165572005")
        await an_account(api_client, "Someone Else's Visa")

        await onboard(api_client, "+14165572006")
        listed = (await api_client.get("/accounts")).json()

        assert listed == []


class TestDedup:
    async def test_the_same_statement_twice_saves_once(
        self, api_client, db_session, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        me = await onboard(api_client, "+14165572007")
        account = await an_account(api_client)
        rows = [row(), row("134.02", "LOBLAWS 1042", "2026-08-03")]

        first = await save(
            api_client,
            await an_import(db_session, me["household"]["id"]),
            account,
            rows,
        )
        second = await save(
            api_client,
            await an_import(db_session, me["household"]["id"]),
            account,
            rows,
        )

        assert first.json()["saved"] == 2
        assert second.json()["saved"] == 0
        assert second.json()["duplicates"] == 2
        total = await db_session.execute(select(Transaction))
        assert len(total.scalars().all()) == 2

    async def test_overlapping_statements_save_the_union_not_the_sum(
        self, api_client, db_session, monkeypatch
    ):
        """January, then January–February. The two weeks they share must not
        land twice."""
        use_model(monkeypatch, FakeModel())
        me = await onboard(api_client, "+14165572008")
        account = await an_account(api_client)
        january = [row(day="2026-01-05"), row("20.00", "NETFLIX", "2026-01-25")]
        overlap = january + [row("9.99", "SPOTIFY", "2026-02-10")]

        await save(
            api_client,
            await an_import(db_session, me["household"]["id"]),
            account,
            january,
        )
        second = await save(
            api_client,
            await an_import(db_session, me["household"]["id"]),
            account,
            overlap,
        )

        assert second.json()["saved"] == 1
        assert second.json()["duplicates"] == 2
        total = await db_session.execute(select(Transaction))
        assert len(total.scalars().all()) == 3

    async def test_a_genuine_repeat_purchase_survives(
        self, api_client, db_session, monkeypatch
    ):
        """Two coffees, same shop, same day, same price. The dedup rule must
        not eat one — and it looks identical to a duplicate, which is why
        occurrence exists."""
        use_model(monkeypatch, FakeModel())
        me = await onboard(api_client, "+14165572009")
        account = await an_account(api_client)

        response = await save(
            api_client,
            await an_import(db_session, me["household"]["id"]),
            account,
            [row(), row()],
        )

        assert response.json()["saved"] == 2
        saved = await db_session.execute(select(Transaction.occurrence))
        assert sorted(saved.scalars().all()) == [1, 2]

    async def test_a_repeat_still_dedups_on_re_import(
        self, api_client, db_session, monkeypatch
    ):
        """Both halves at once: two real coffees, then the same statement
        again. Two saved, then nothing."""
        use_model(monkeypatch, FakeModel())
        me = await onboard(api_client, "+14165572010")
        account = await an_account(api_client)

        await save(
            api_client,
            await an_import(db_session, me["household"]["id"]),
            account,
            [row(), row()],
        )
        again = await save(
            api_client,
            await an_import(db_session, me["household"]["id"]),
            account,
            [row(), row()],
        )

        assert again.json()["saved"] == 0
        assert again.json()["duplicates"] == 2

    async def test_a_better_parse_flags_rather_than_doubles(
        self, api_client, db_session, monkeypatch
    ):
        """The case the near-match check exists for.

        We improve the parser, the user re-imports, and the same purchase now
        carries a different description — so the unique key cannot see it. It
        must be flagged and pointed at what it matched, not silently doubled.
        """
        use_model(monkeypatch, FakeModel())
        me = await onboard(api_client, "+14165572011")
        account = await an_account(api_client)
        await save(
            api_client,
            await an_import(db_session, me["household"]["id"]),
            account,
            [row(description="POS PURCHASE HORTONS 4821 REF 99812")],
        )

        better = await save(
            api_client,
            await an_import(db_session, me["household"]["id"]),
            account,
            [row(description="TIM HORTONS")],
        )

        assert better.json()["saved"] == 1
        assert better.json()["flagged"] == 1
        flagged = await db_session.execute(
            select(Transaction).where(Transaction.duplicate_of_id.isnot(None))
        )
        row_ = flagged.scalar_one()
        assert row_.needs_review is True
        assert row_.review_reason is ReviewReason.suspected_duplicate

    async def test_two_payments_on_one_statement_never_flag_each_other(
        self, api_client, db_session, monkeypatch
    ):
        """Two $100 e-transfers to different people, same day, one statement.

        Neither is a duplicate of the other — the statement listed them both,
        so both happened. Without excluding the current import from the
        near-match, every pair of same-day round amounts flags itself, and a
        review queue that is mostly false alarms is one people stop opening.
        """
        use_model(monkeypatch, FakeModel())
        me = await onboard(api_client, "+14165572024")
        account = await an_account(api_client)

        response = await save(
            api_client,
            await an_import(db_session, me["household"]["id"]),
            account,
            [
                row("100.00", "E-TRANSFER TO ALEX", "2026-08-02"),
                row("100.00", "E-TRANSFER TO SAM", "2026-08-02"),
            ],
        )

        assert response.json()["saved"] == 2
        assert response.json()["flagged"] == 0
        flagged = await db_session.execute(
            select(Transaction).where(Transaction.duplicate_of_id.isnot(None))
        )
        assert flagged.scalars().all() == []

    async def test_amounts_round_trip_exactly(
        self, api_client, db_session, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        me = await onboard(api_client, "+14165572012")
        account = await an_account(api_client)

        await save(
            api_client,
            await an_import(db_session, me["household"]["id"]),
            account,
            [row("2410.00", "PAYROLL"), row("0.42", "INTEREST PAID")],
        )

        stored = await db_session.execute(select(Transaction.amount_minor_units))
        assert sorted(stored.scalars().all()) == [42, 241000]

    async def test_every_stored_key_matches_the_current_normalizer(
        self, api_client, db_session, monkeypatch
    ):
        """The backfill guard. Change `normalized` without recomputing what is
        stored and dedup stops working against the whole existing corpus — not
        just against new imports, and with nothing raising."""
        use_model(monkeypatch, FakeModel())
        me = await onboard(api_client, "+14165572013")
        account = await an_account(api_client)
        await save(
            api_client,
            await an_import(db_session, me["household"]["id"]),
            account,
            [row(), row("134.02", "POS PURCHASE LOBLAWS 1042", "2026-08-03")],
        )

        stored = await db_session.execute(
            select(Transaction.description, Transaction.normalized_description)
        )
        for description, key in stored.all():
            assert key == normalized(description)


class TestWhatReachesTheModel:
    async def test_only_merchants_and_amounts(
        self, api_client, db_session, monkeypatch
    ):
        """PRD Appendix A.3, asserted on the wire rather than promised."""
        model = FakeModel()
        use_model(monkeypatch, model)
        me = await onboard(api_client, "+14165572014")
        account = await an_account(api_client)

        await save(
            api_client,
            await an_import(db_session, me["household"]["id"]),
            account,
            [row(description="TIM HORTONS #4821 OTTAWA ON")],
        )

        sent = model.prompts[-1]
        assert "Tim Hortons" in sent
        for forbidden in ("2026-08-02", account, "4821", "#", "debit"):
            assert forbidden not in sent, f"{forbidden!r} reached the model"

    async def test_an_invented_category_becomes_other_and_is_flagged(
        self, api_client, db_session, monkeypatch
    ):
        use_model(monkeypatch, FakeModel(answer='["artisanal_coffee"]'))
        me = await onboard(api_client, "+14165572015")
        account = await an_account(api_client)

        await save(
            api_client,
            await an_import(db_session, me["household"]["id"]),
            account,
            [row()],
        )

        saved = await db_session.execute(select(Transaction))
        transaction = saved.scalar_one()
        assert transaction.needs_review is True
        assert transaction.review_reason is ReviewReason.unknown_category
        invented = await db_session.execute(
            select(Category).where(Category.slug == "artisanal_coffee")
        )
        assert invented.scalar_one_or_none() is None, "a model minted a category"

    async def test_one_household_s_corrections_never_reach_another_s_prompt(
        self, api_client, db_session, monkeypatch
    ):
        """Appendix A.5 #6: per-household personalization, never pooled."""
        model = FakeModel()
        use_model(monkeypatch, model)
        me = await onboard(api_client, "+14165572016")
        shopping = await db_session.execute(
            select(Category.id).where(Category.slug == "shopping")
        )
        db_session.add(
            CategoryCorrection(
                household_id=uuid.UUID(me["household"]["id"]),
                merchant_pattern="canadian tire",
                corrected_category_id=shopping.scalar_one(),
            )
        )
        await db_session.flush()

        other = await onboard(api_client, "+14165572017")
        account = await an_account(api_client)
        await save(
            api_client,
            await an_import(db_session, other["household"]["id"]),
            account,
            [row()],
        )

        assert "canadian tire" not in model.systems[-1].lower()


class TestIsolation:
    async def test_another_household_s_import_is_not_found(
        self, api_client, db_session, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        me = await onboard(api_client, "+14165572018")
        theirs = await an_import(db_session, me["household"]["id"])

        await onboard(api_client, "+14165572019")
        account = await an_account(api_client)
        response = await save(api_client, theirs, account, [row()])

        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "unknown_import"

    async def test_an_account_from_another_household_is_not_found(
        self, api_client, db_session, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        await onboard(api_client, "+14165572020")
        theirs = await an_account(api_client)

        me = await onboard(api_client, "+14165572021")
        response = await save(
            api_client,
            await an_import(db_session, me["household"]["id"]),
            theirs,
            [row()],
        )

        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "unknown_account"


class TestTheImportRecord:
    async def test_it_reports_how_the_import_turned_out(
        self, api_client, db_session, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        me = await onboard(api_client, "+14165572022")
        account = await an_account(api_client)
        import_id = await an_import(db_session, me["household"]["id"])
        await save(api_client, import_id, account, [row(), row(confidence=10)])

        report = (await api_client.get(f"/statements/{import_id}")).json()

        assert report["saved"] == 2
        assert report["needs_review"] == 1
        # Not confirmed: one row is still flagged, and an import nobody has
        # looked at is not a finished one. 3.4 stamps it as the last review is
        # resolved.
        assert report["confirmed_at"] is None

    async def test_an_import_with_rows_still_flagged_is_not_confirmed(
        self, api_client, db_session, monkeypatch
    ):
        """`confirmed_at` means the user has finished with this import. Stamping
        it while rows are still flagged marks a statement complete that nobody
        has looked at — 3.4 reads this to decide when an import is done."""
        use_model(monkeypatch, FakeModel())
        me = await onboard(api_client, "+14165572025")
        account = await an_account(api_client)
        import_id = await an_import(db_session, me["household"]["id"])

        await save(api_client, import_id, account, [row(confidence=10)])

        report = (await api_client.get(f"/statements/{import_id}")).json()
        assert report["needs_review"] == 1
        assert report["confirmed_at"] is None

    async def test_an_import_with_nothing_outstanding_is_confirmed(
        self, api_client, db_session, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        me = await onboard(api_client, "+14165572026")
        account = await an_account(api_client)
        import_id = await an_import(db_session, me["household"]["id"])

        await save(api_client, import_id, account, [row()])

        report = (await api_client.get(f"/statements/{import_id}")).json()
        assert report["needs_review"] == 0
        assert report["confirmed_at"] is not None

    async def test_a_missing_model_key_does_not_lose_the_import(
        self, api_client, db_session, monkeypatch
    ):
        """Rows are written before categorization precisely so a model problem
        costs nothing. A misconfigured key must not be the exception."""
        from app.services.llm import LlmError as _LlmError

        def unconfigured(_settings):
            raise _LlmError("LLM_API_KEY is not set")

        monkeypatch.setattr(endpoint, "build_client", unconfigured)
        me = await onboard(api_client, "+14165572027")
        account = await an_account(api_client)

        response = await save(
            api_client,
            await an_import(db_session, me["household"]["id"]),
            account,
            [row()],
        )

        assert response.status_code == 200, response.text
        assert response.json()["saved"] == 1
        saved = await db_session.execute(select(Transaction))
        assert saved.scalar_one().category_id is None

    async def test_a_low_confidence_row_goes_to_review(
        self, api_client, db_session, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        me = await onboard(api_client, "+14165572023")
        account = await an_account(api_client)
        await save(
            api_client,
            await an_import(db_session, me["household"]["id"]),
            account,
            [row(confidence=20)],
        )

        saved = await db_session.execute(select(Transaction))
        assert saved.scalar_one().review_reason is ReviewReason.low_confidence
