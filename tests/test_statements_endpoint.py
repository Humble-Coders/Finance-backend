"""`POST /statements/parse` — the refusals, the record, and what is kept.

Every test rolls back. The model is faked: what matters here is the order of the
gates and the fact that a refused request never reaches a paid provider.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select, text

from app.api import statements as endpoint
from app.auth import AuthenticatedUser, current_user
from app.models.enums import StatementImportStatus
from app.models.money import StatementImport, StatementImportText
from app.services.llm import LlmError
from tests.conftest import requires_db

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

PARSE = "/statements/parse"

STATEMENT = "\n".join(
    [
        "2026-08-14  TIM HORTONS #4821            12.40",
        "2026-08-15  LOBLAWS 1234                134.02",
    ]
)

GOOD_ANSWER = json.dumps(
    [
        {
            "date": "2026-08-14",
            "description": "TIM HORTONS",
            "amount": "12.40",
            "direction": "debit",
            "confidence": 96,
        },
        {
            "date": "2026-08-15",
            "description": "LOBLAWS",
            "amount": "134.02",
            "direction": "debit",
            "confidence": 93,
        },
    ]
)

# Every amount here is absent from STATEMENT, so every row is rejected — what a
# genuinely unreadable document looks like from this endpoint's side.
INVENTED_ANSWER = json.dumps(
    [
        {
            "date": "2026-08-14",
            "description": "SOMETHING",
            "amount": "77.77",
            "direction": "debit",
            "confidence": 40,
        }
    ]
)


class FakeModel:
    def __init__(self, answer: str = GOOD_ANSWER) -> None:
        self.answer = answer
        self.calls = 0

    @property
    def model(self) -> str:
        return "fake-1"

    async def complete(self, *, system: str, user: str, max_output_tokens: int) -> str:
        self.calls += 1
        return self.answer


def authenticate_as(*, phone: str) -> None:
    from app.main import app

    app.dependency_overrides[current_user] = lambda: AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        email=None,
        phone=phone,
        claims={"app_metadata": {"provider": "phone"}},
    )


def use_model(monkeypatch, model) -> None:
    monkeypatch.setattr(endpoint, "build_client", lambda _settings: model)


async def onboard(api_client, phone: str) -> None:
    """Past the account terms. AI consent is deliberately NOT given here."""
    authenticate_as(phone=phone)
    await api_client.get("/me")
    version = (await api_client.get("/legal/terms")).json()["version"]
    await api_client.post("/me/consent", json={"version": version})


async def consent_to_ai(api_client) -> None:
    version = (await api_client.get("/legal/ai-processing")).json()["version"]
    response = await api_client.post(
        "/legal/ai-processing/consent", json={"version": version}
    )
    assert response.status_code == 200, response.text


def body(**overrides) -> dict:
    return {"source_kind": "pdf_text", "page_count": 2, "text": STATEMENT, **overrides}


class TestConsent:
    async def test_refused_before_consent_and_the_model_is_never_called(
        self, api_client, monkeypatch
    ):
        model = FakeModel()
        use_model(monkeypatch, model)
        await onboard(api_client, "+14165571001")

        response = await api_client.post(PARSE, json=body())

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "ai_consent_required"
        assert model.calls == 0, "a refused request must not reach a paid provider"

    async def test_allowed_once_consent_is_recorded(self, api_client, monkeypatch):
        use_model(monkeypatch, FakeModel())
        await onboard(api_client, "+14165571002")
        await consent_to_ai(api_client)

        response = await api_client.post(PARSE, json=body())

        assert response.status_code == 200, response.text
        assert [row["amount"] for row in response.json()["rows"]] == ["12.40", "134.02"]

    async def test_consent_to_a_stale_version_is_refused(self, api_client):
        await onboard(api_client, "+14165571003")

        response = await api_client.post(
            "/legal/ai-processing/consent", json={"version": "ai-v0"}
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "ai_policy_version_mismatch"


class TestQuota:
    async def test_the_second_import_in_a_month_is_refused(
        self, api_client, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        await onboard(api_client, "+14165571004")
        await consent_to_ai(api_client)

        first = await api_client.post(PARSE, json=body())
        second = await api_client.post(PARSE, json=body())

        assert first.status_code == 200
        assert second.status_code == 429
        detail = second.json()["detail"]
        assert detail["code"] == "import_quota_exceeded"
        assert detail["resets_at"], "a limit must say when it lifts"


class TestFeatureGate:
    async def test_403_before_any_model_call_when_the_feature_is_off(
        self, api_client, db_session, monkeypatch
    ):
        model = FakeModel()
        use_model(monkeypatch, model)
        await onboard(api_client, "+14165571005")
        await consent_to_ai(api_client)
        await db_session.execute(
            text(
                "UPDATE feature_availability SET is_enabled = false "
                "WHERE feature_key = 'document_upload'"
            )
        )

        response = await api_client.post(PARSE, json=body())

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "feature_unavailable"
        assert model.calls == 0


class TestTheImportRecord:
    async def test_a_successful_import_keeps_no_text(
        self, api_client, db_session, monkeypatch
    ):
        """The default has to be the safe one even if the opt-in is buggy."""
        use_model(monkeypatch, FakeModel())
        await onboard(api_client, "+14165571006")
        await consent_to_ai(api_client)

        response = await api_client.post(PARSE, json=body())

        assert response.json()["text_retained_until"] is None
        kept = await db_session.execute(select(StatementImportText))
        assert kept.scalars().all() == []

    async def test_asking_to_keep_the_text_is_ignored_when_the_parse_worked(
        self, api_client, db_session, monkeypatch
    ):
        """Consent was for "help us fix what went wrong" — honouring it after a
        clean parse would make a diagnostic into routine collection."""
        use_model(monkeypatch, FakeModel())
        await onboard(api_client, "+14165571007")
        await consent_to_ai(api_client)

        response = await api_client.post(
            PARSE, json=body(keep_text_for_diagnostics=True)
        )

        assert response.json()["text_retained_until"] is None
        kept = await db_session.execute(select(StatementImportText))
        assert kept.scalars().all() == []

    async def test_the_text_is_kept_when_asked_and_the_parse_went_badly(
        self, api_client, db_session, monkeypatch
    ):
        use_model(monkeypatch, FakeModel(INVENTED_ANSWER))
        await onboard(api_client, "+14165571008")
        await consent_to_ai(api_client)

        response = await api_client.post(
            PARSE, json=body(keep_text_for_diagnostics=True)
        )

        assert response.json()["rows"] == []
        assert response.json()["text_retained_until"] is not None
        kept = (await db_session.execute(select(StatementImportText))).scalars().all()
        assert len(kept) == 1
        assert kept[0].text == STATEMENT

    async def test_a_model_failure_marks_the_import_failed_and_returns_502(
        self, api_client, db_session, monkeypatch
    ):
        class Broken(FakeModel):
            async def complete(self, **_kwargs):
                raise LlmError("provider returned 503")

        use_model(monkeypatch, Broken())
        await onboard(api_client, "+14165571009")
        await consent_to_ai(api_client)

        response = await api_client.post(PARSE, json=body())

        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "parse_failed"
        record = (await db_session.execute(select(StatementImport))).scalars().one()
        assert record.status is StatementImportStatus.failed
        assert record.failure_reason

    async def test_an_account_from_another_household_is_not_found(
        self, api_client, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        await onboard(api_client, "+14165571010")
        await consent_to_ai(api_client)

        response = await api_client.post(PARSE, json=body(account_id=str(uuid.uuid4())))

        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "unknown_account"
