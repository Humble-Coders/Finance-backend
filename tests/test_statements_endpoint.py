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


class TestTheNoTrainingClaim:
    """The consent screen states as fact that the provider may not train on
    this data. Production refuses to parse until someone asserts that is true.

    M3 develops against a free tier, whose terms generally permit exactly what
    the screen says is forbidden. Showing a user that text and sending their
    statement anyway is the violation itself (PRD Appendix A.2), not a step
    towards it.
    """

    async def test_production_refuses_until_the_tier_is_confirmed(
        self, api_client, monkeypatch
    ):
        from app.config import Settings, get_settings

        model = FakeModel()
        use_model(monkeypatch, model)
        await onboard(api_client, "+14165571019")
        await consent_to_ai(api_client)

        live = get_settings()
        unconfirmed = Settings(
            **{
                **live.model_dump(),
                "app_env": "production",
                "llm_no_training_tier": False,
            }
        )
        monkeypatch.setattr(endpoint, "get_settings", lambda: unconfirmed)

        response = await api_client.post(PARSE, json=body())

        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "ai_processing_unavailable"
        assert model.calls == 0, "nothing may be sent before the tier is confirmed"

    async def test_production_parses_once_it_is_confirmed(
        self, api_client, monkeypatch
    ):
        from app.config import Settings, get_settings

        use_model(monkeypatch, FakeModel())
        await onboard(api_client, "+14165571020")
        await consent_to_ai(api_client)

        live = get_settings()
        confirmed = Settings(
            **{
                **live.model_dump(),
                "app_env": "production",
                "llm_no_training_tier": True,
            }
        )
        monkeypatch.setattr(endpoint, "get_settings", lambda: confirmed)

        response = await api_client.post(PARSE, json=body())

        assert response.status_code == 200, response.text


class TestConsent:
    async def test_refused_before_consent_and_the_model_is_never_called(
        self, api_client, monkeypatch
    ):
        model = FakeModel()
        use_model(monkeypatch, model)
        await onboard(api_client, "+14165571001")

        response = await api_client.post(PARSE, json=body())

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "consent_required"
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
    async def test_a_failed_import_does_not_cost_the_month(
        self, api_client, monkeypatch
    ):
        """The 502 says "please try again". This is what makes that true.

        Counting a failure against the free tier's single monthly import meant a
        statement we could not read cost someone their month — and the retry the
        message asked for came back 429. Worse for exactly the users who then
        opted in to send us the text so we could fix it.
        """

        class Broken(FakeModel):
            async def complete(self, **_kwargs):
                raise LlmError("provider returned 503")

        await onboard(api_client, "+14165571011")
        await consent_to_ai(api_client)

        use_model(monkeypatch, Broken())
        failed = await api_client.post(PARSE, json=body())
        use_model(monkeypatch, FakeModel())
        retried = await api_client.post(PARSE, json=body())

        assert failed.status_code == 502
        assert retried.status_code == 200, retried.text

    async def test_a_parse_that_finds_nothing_does_not_cost_the_month_either(
        self, api_client, monkeypatch
    ):
        """An import that returns no rows is a failure to the person holding a
        statement full of transactions, whatever the model thought."""
        await onboard(api_client, "+14165571012")
        await consent_to_ai(api_client)

        use_model(monkeypatch, FakeModel(INVENTED_ANSWER))
        empty = await api_client.post(PARSE, json=body())
        use_model(monkeypatch, FakeModel())
        retried = await api_client.post(PARSE, json=body())

        assert empty.status_code == 200
        assert empty.json()["rows"] == []
        assert retried.status_code == 200, retried.text

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
    async def test_the_response_says_which_currency_the_amounts_are_in(
        self, api_client, monkeypatch
    ):
        """Money is an amount *and* a currency (PRD §4.4). A client left to
        infer it from /capabilities formats "12.40" with whatever symbol it
        guessed — an error nobody reports and everybody notices."""
        use_model(monkeypatch, FakeModel())
        await onboard(api_client, "+14165571018")
        await consent_to_ai(api_client)

        response = await api_client.post(PARSE, json=body())

        assert response.json()["currency"] == "CAD"

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

    async def test_a_statement_too_long_to_read_says_so_with_413(
        self, api_client, monkeypatch
    ):
        from app.services.statements import MAX_TEXT_CHARS

        use_model(monkeypatch, FakeModel())
        await onboard(api_client, "+14165571013")
        await consent_to_ai(api_client)

        response = await api_client.post(
            PARSE, json=body(text="x" * (MAX_TEXT_CHARS + 1))
        )

        assert response.status_code == 413
        assert response.json()["detail"]["code"] == "statement_too_long"
        # The refusal must not quote the statement back, same as a 422.
        assert "xxxx" not in response.text

    async def test_a_future_dated_policy_is_not_yet_in_force(
        self, api_client, db_session, monkeypatch
    ):
        """Announcing next month's policy must not invalidate today's consent.

        Without the `effective_from <= now()` test this passed the moment it was
        inserted: every existing consent void, every import refused, and the
        text users were pointed at not live yet.
        """
        use_model(monkeypatch, FakeModel())
        await onboard(api_client, "+14165571014")
        await consent_to_ai(api_client)
        await db_session.execute(
            text(
                "INSERT INTO disclaimer_version "
                "(id, country_code, version, kind, body, effective_from, "
                " created_at, updated_at) "
                "VALUES (gen_random_uuid(), NULL, 'ai-v2', 'ai_processing', "
                "'later', now() + interval '30 days', now(), now())"
            )
        )

        response = await api_client.post(PARSE, json=body())

        assert response.status_code == 200, response.text
        assert (await api_client.get("/legal/ai-processing")).json()[
            "version"
        ] == "ai-v1"

    async def test_expired_text_is_purged_even_when_the_request_is_refused(
        self, api_client, db_session, monkeypatch
    ):
        """The purge runs before the gates — which is worth nothing unless it
        commits, because `get_session` never commits on its own and every
        refusal path raises."""
        use_model(monkeypatch, FakeModel(INVENTED_ANSWER))
        await onboard(api_client, "+14165571015")
        await consent_to_ai(api_client)
        await api_client.post(PARSE, json=body(keep_text_for_diagnostics=True))
        await db_session.execute(
            text(
                "UPDATE statement_import_text SET expires_at = now() - interval '1 day'"
            )
        )

        # Refused for size, so nothing in this request commits by itself.
        refused = await api_client.post(PARSE, json=body(text="x" * 300_000))

        assert refused.status_code == 413
        kept = (await db_session.execute(select(StatementImportText))).scalars().all()
        assert kept == [], "expired text survived a refused request"

    async def test_a_missing_api_key_is_a_502_not_a_500(self, api_client, monkeypatch):
        """The failure mode of the free-tier-to-paid swap. It must land as the
        error the client already handles, with the import recorded as failed."""
        from app.services.llm import LlmError as _LlmError

        def unconfigured(_settings):
            raise _LlmError("LLM_API_KEY is not set")

        monkeypatch.setattr(endpoint, "build_client", unconfigured)
        await onboard(api_client, "+14165571016")
        await consent_to_ai(api_client)

        response = await api_client.post(PARSE, json=body())

        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "parse_failed"

    async def test_too_many_transactions_is_413_and_does_not_cost_the_month(
        self, api_client, db_session, monkeypatch
    ):
        """A statement read perfectly, with more in it than we import at once.

        Its own code, because "we could not read that statement" is both wrong
        and unactionable here — and it must not burn the month's import, since
        nothing was imported.
        """
        from app.services import statements as parsing

        use_model(monkeypatch, FakeModel())
        await onboard(api_client, "+14165571017")
        await consent_to_ai(api_client)

        monkeypatch.setattr(parsing, "MAX_ROWS", 1)
        refused = await api_client.post(PARSE, json=body())
        # Lift the limit before retrying, or the retry fails for the same
        # reason and proves nothing about the quota.
        monkeypatch.setattr(parsing, "MAX_ROWS", 2_000)
        retried = await api_client.post(PARSE, json=body())

        assert refused.status_code == 413
        assert refused.json()["detail"]["code"] == "too_many_transactions"
        # By id, not `.first()`: this test makes two imports — the refused one
        # and the retry — and an unordered SELECT returns whichever Postgres
        # feels like. The assertion passed or failed depending on the day,
        # about two runs in five.
        refused_id = uuid.UUID(refused.json()["detail"]["import_id"])
        record = (
            await db_session.execute(
                select(StatementImport).where(StatementImport.id == refused_id)
            )
        ).scalar_one()
        assert record.status is StatementImportStatus.failed
        assert retried.status_code == 200, "a refusal must not cost the month"

    async def test_an_account_from_another_household_is_not_found(
        self, api_client, monkeypatch
    ):
        use_model(monkeypatch, FakeModel())
        await onboard(api_client, "+14165571010")
        await consent_to_ai(api_client)

        response = await api_client.post(PARSE, json=body(account_id=str(uuid.uuid4())))

        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "unknown_account"
