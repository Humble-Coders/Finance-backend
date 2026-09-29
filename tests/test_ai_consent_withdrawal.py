"""Withdrawing consent to AI processing, and giving it again (#42).

PIPEDA gives the right to withdraw consent at any time. These hold the three
promises that right needs: withdrawal really stops the processing, the consent
that was given stays provable, and consent can come back — against a real
Postgres, counting rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.models.enums import (
    ConsentAction,
    PolicyKind,
    TransactionDirection,
    TransactionSource,
)
from app.models.identity import ConsentChange, ConsentEvent, User
from app.models.money import Transaction
from app.models.platform import DisclaimerVersion
from tests.conftest import requires_db
from tests.test_ledger_endpoint import FakeModel as CategorizingModel
from tests.test_ledger_endpoint import use_model as use_categorizer
from tests.test_statements_endpoint import (
    PARSE,
    FakeModel,
    body,
    consent_to_ai,
    onboard,
    use_model,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

CONSENT = "/legal/ai-processing/consent"


async def the_user(db_session, phone: str) -> User:
    return (
        await db_session.execute(select(User).where(User.phone == phone))
    ).scalar_one()


async def changes(db_session, user: User) -> list[ConsentAction]:
    result = await db_session.execute(
        select(ConsentChange.action)
        .where(ConsentChange.user_id == user.id)
        .order_by(ConsentChange.created_at)
    )
    return list(result.scalars().all())


async def consent_events(db_session, user: User) -> int:
    result = await db_session.execute(
        select(func.count())
        .select_from(ConsentEvent)
        .join(
            DisclaimerVersion,
            DisclaimerVersion.id == ConsentEvent.disclaimer_version_id,
        )
        .where(
            ConsentEvent.user_id == user.id,
            DisclaimerVersion.kind == PolicyKind.ai_processing,
        )
    )
    return result.scalar_one()


class TestWithdrawing:
    async def test_after_withdrawal_parsing_is_refused_and_no_model_is_called(
        self, api_client, monkeypatch
    ):
        model = FakeModel()
        use_model(monkeypatch, model)
        await onboard(api_client, "+14165591001")
        await consent_to_ai(api_client)

        withdrawn = await api_client.delete(CONSENT)
        response = await api_client.post(PARSE, json=body())

        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.json()["consented"] is False
        assert response.status_code == 409
        # The code the app already routes to its consent screen — no client change.
        assert response.json()["detail"]["code"] == "consent_required"
        assert model.calls == 0

    async def test_the_consent_given_is_still_there_after_withdrawal(
        self, api_client, db_session
    ):
        phone = "+14165591002"
        await onboard(api_client, phone)
        await consent_to_ai(api_client)

        await api_client.delete(CONSENT)

        user = await the_user(db_session, phone)
        # Given, then withdrawn — not an absence. The row proving what was
        # agreed to is untouched.
        assert await consent_events(db_session, user) == 1
        assert await changes(db_session, user) == [
            ConsentAction.given,
            ConsentAction.withdrawn,
        ]

    async def test_consent_given_before_withdrawal_existed_can_be_withdrawn(
        self, api_client, db_session
    ):
        # Consent recorded before `consent_change` existed has only its
        # `consent_event` row. Withdrawing it must still work.
        phone = "+14165591003"
        await onboard(api_client, phone)
        user = await the_user(db_session, phone)
        policy = (
            await db_session.execute(
                select(DisclaimerVersion).where(DisclaimerVersion.version == "ai-v1")
            )
        ).scalar_one()
        db_session.add(ConsentEvent(user_id=user.id, disclaimer_version_id=policy.id))
        await db_session.flush()
        assert (await api_client.get(CONSENT)).json()["consented"] is True

        await api_client.delete(CONSENT)

        assert (await api_client.get(CONSENT)).json()["consented"] is False
        assert await changes(db_session, user) == [ConsentAction.withdrawn]


class TestIdempotence:
    async def test_withdrawing_without_ever_consenting_is_a_quiet_no_op(
        self, api_client, db_session
    ):
        phone = "+14165591010"
        await onboard(api_client, phone)

        response = await api_client.delete(CONSENT)

        assert response.status_code == 200
        assert response.json()["consented"] is False
        # Nothing to withdraw, so nothing recorded as withdrawn.
        assert await changes(db_session, await the_user(db_session, phone)) == []

    async def test_withdrawing_twice_records_one_withdrawal(
        self, api_client, db_session
    ):
        phone = "+14165591011"
        await onboard(api_client, phone)
        await consent_to_ai(api_client)

        first = await api_client.delete(CONSENT)
        second = await api_client.delete(CONSENT)

        assert (first.status_code, second.status_code) == (200, 200)
        assert await changes(db_session, await the_user(db_session, phone)) == [
            ConsentAction.given,
            ConsentAction.withdrawn,
        ]

    async def test_agreeing_twice_records_one_consent(self, api_client, db_session):
        phone = "+14165591012"
        await onboard(api_client, phone)

        await consent_to_ai(api_client)
        await consent_to_ai(api_client)

        user = await the_user(db_session, phone)
        assert await consent_events(db_session, user) == 1
        assert await changes(db_session, user) == [ConsentAction.given]


class TestConsentingAgain:
    async def test_re_consent_works_and_the_log_shows_the_whole_sequence(
        self, api_client, db_session, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        phone = "+14165591020"
        await onboard(api_client, phone)
        await consent_to_ai(api_client)
        await api_client.delete(CONSENT)

        await consent_to_ai(api_client)
        response = await api_client.post(PARSE, json=body())

        assert response.status_code == 200, response.text
        user = await the_user(db_session, phone)
        # The same version agreed to twice: still one proof of the text,
        # and the sequence alongside it.
        assert await consent_events(db_session, user) == 1
        assert await changes(db_session, user) == [
            ConsentAction.given,
            ConsentAction.withdrawn,
            ConsentAction.given,
        ]


class TestWhatWithdrawalDoesNotTouch:
    async def test_transactions_imported_before_are_untouched(
        self, api_client, db_session
    ):
        from tests.test_ledger_endpoint import an_account

        await onboard(api_client, "+14165591030")
        await consent_to_ai(api_client)
        account = uuid.UUID(await an_account(api_client))
        household = (
            await db_session.execute(
                select(User.household_id).where(User.phone == "+14165591030")
            )
        ).scalar_one()
        row = Transaction(
            household_id=household,
            account_id=account,
            occurred_on=datetime.now(UTC).date() - timedelta(days=4),
            amount_minor_units=1240,
            currency="CAD",
            direction=TransactionDirection.debit,
            description="TIM HORTONS",
            normalized_description="tim hortons",
            source=TransactionSource.upload,
        )
        db_session.add(row)
        await db_session.flush()
        before = (row.id, row.amount_minor_units, row.description, row.category_id)

        await api_client.delete(CONSENT)

        await db_session.refresh(row)
        assert (
            row.id,
            row.amount_minor_units,
            row.description,
            row.category_id,
        ) == before

    async def test_one_person_s_withdrawal_leaves_another_s_consent_alone(
        self, api_client, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        await onboard(api_client, "+14165591031")
        await consent_to_ai(api_client)
        await onboard(api_client, "+14165591032")
        await consent_to_ai(api_client)

        await onboard(api_client, "+14165591031")
        await api_client.delete(CONSENT)

        await onboard(api_client, "+14165591032")
        assert (await api_client.get(CONSENT)).json()["consented"] is True
        response = await api_client.post(PARSE, json=body())
        assert response.status_code == 200, response.text

    async def test_a_typed_in_entry_is_no_longer_sent_to_the_model(
        self, api_client, monkeypatch
    ):
        # #38: manual entry asks the model only with consent. Withdrawal ends it.
        from tests.test_manual_entry import a_household, add, entry

        model = CategorizingModel(answer='["dining"]')
        use_categorizer(monkeypatch, model)
        _, account = await a_household(api_client, "+14165591033")
        await consent_to_ai(api_client)
        await api_client.delete(CONSENT)

        response = await add(api_client, entry(account))

        assert response.status_code == 201, response.text
        assert model.prompts == []
        assert response.json()["review_reason"] == "unknown_category"


class TestStatus:
    async def test_it_follows_each_change(self, api_client):
        await onboard(api_client, "+14165591040")

        never = (await api_client.get(CONSENT)).json()
        await consent_to_ai(api_client)
        given = (await api_client.get(CONSENT)).json()
        await api_client.delete(CONSENT)
        withdrawn = (await api_client.get(CONSENT)).json()

        assert [never["consented"], given["consented"], withdrawn["consented"]] == [
            False,
            True,
            False,
        ]
        assert never["version"] == "ai-v1"

    async def test_a_new_policy_in_force_needs_consent_again(
        self, api_client, db_session
    ):
        # Consent to old text is not consent to the new one — the reason ai-v2
        # is seeded as a draft rather than in force.
        await onboard(api_client, "+14165591041")
        await consent_to_ai(api_client)
        await db_session.execute(
            update(DisclaimerVersion)
            .where(DisclaimerVersion.version == "ai-v2")
            .values(effective_from=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db_session.flush()

        status = (await api_client.get(CONSENT)).json()

        assert status == {"consented": False, "version": "ai-v2"}


class TestThePolicyText:
    async def test_ai_v2_is_a_draft_and_ai_v1_stays_in_force(
        self, api_client, db_session
    ):
        await onboard(api_client, "+14165591050")

        shown = (await api_client.get("/legal/ai-processing")).json()
        draft = (
            await db_session.execute(
                select(DisclaimerVersion).where(DisclaimerVersion.version == "ai-v2")
            )
        ).scalar_one()

        assert shown["version"] == "ai-v1"
        assert draft.effective_from is None
        assert draft.kind is PolicyKind.ai_processing

    async def test_ai_v2_says_what_withdrawal_does_and_what_is_sent(self, db_session):
        text = (
            await db_session.execute(
                select(DisclaimerVersion.body).where(
                    DisclaimerVersion.version == "ai-v2"
                )
            )
        ).scalar_one()

        assert "withdraw this consent at any time" in text
        assert "you cannot import statements" in text
        assert "does not delete the transactions you have already saved" in text
        # #38: typed-in entries are covered, and only name and amount go.
        assert "shop name and amount are sent" in text
        # Account deletion is not built, so the policy must not point at it.
        assert "delet" not in text.replace("does not delete", "")
