"""Every 409 leaves a line saying which one it was.

No database: these prove the logging, and the logging is pure. The endpoint
suites already prove the conflicts themselves are raised in the right places.

The point of the suite is the *negative* assertion as much as the positive one.
A conflict is raised over a phone number or an email address, and the obvious
thing to log is the value that collided — which is exactly the thing
CLAUDE.md → Privacy forbids. So each test checks what reached the log AND what
did not.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
import structlog

from app.api import deps
from app.auth import AuthenticatedUser
from app.services.identity import (
    PHONE_TAKEN_ON_CHECK,
    PHONE_TAKEN_ON_CREATE,
    PHONE_TAKEN_ON_FLUSH,
    PhoneAlreadyLinkedError,
)
from app.services.onboarding import ONBOARDING_REQUIRED, onboarding_conflict

PHONE = "+14165550100"
EMAIL = "someone@example.com"


class FakeSession:
    """Enough of AsyncSession for `current_identity`'s failure path."""

    def __init__(self) -> None:
        self.rolled_back = False

    async def rollback(self) -> None:
        self.rolled_back = True

    async def commit(self) -> None:  # pragma: no cover - not reached here
        raise AssertionError("a conflict must not commit")


def _caller() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="auth-uid-1", email=EMAIL, phone=PHONE, claims={"sub": "auth-uid-1"}
    )


class TestPhoneAlreadyLinked:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_logs_the_code_and_the_path_that_refused(self, monkeypatch):
        async def refuse(session, caller):
            raise PhoneAlreadyLinkedError(caller.phone, PHONE_TAKEN_ON_CHECK)

        monkeypatch.setattr(deps, "resolve_user", refuse)
        session = FakeSession()

        with structlog.testing.capture_logs() as logs:
            with pytest.raises(Exception) as raised:
                await deps.current_identity(caller=_caller(), session=session)

        assert raised.value.status_code == 409
        assert session.rolled_back
        (line,) = [entry for entry in logs if entry["event"] == "conflict"]
        assert line["code"] == deps.PHONE_ALREADY_LINKED
        assert line["reason"] == PHONE_TAKEN_ON_CHECK
        assert line["auth_user_id"] == "auth-uid-1"

    @pytest.mark.asyncio(loop_scope="session")
    async def test_the_number_and_the_address_never_reach_the_log(self, monkeypatch):
        """The whole reason this module exists is that it is tempting to log it."""

        async def refuse(session, caller):
            raise PhoneAlreadyLinkedError(caller.phone, PHONE_TAKEN_ON_FLUSH)

        monkeypatch.setattr(deps, "resolve_user", refuse)

        with structlog.testing.capture_logs() as logs:
            with pytest.raises(Exception):
                await deps.current_identity(caller=_caller(), session=FakeSession())

        rendered = repr(logs)
        assert PHONE not in rendered
        assert EMAIL not in rendered

    def test_each_path_is_named_differently(self):
        """Three situations, one client-facing code — the log is what separates
        a real duplicate from a race, so the names must not collide."""
        names = {PHONE_TAKEN_ON_CHECK, PHONE_TAKEN_ON_FLUSH, PHONE_TAKEN_ON_CREATE}
        assert len(names) == 3


class TestOnboardingRequired:
    def test_logs_the_gate_and_the_outstanding_steps(self):
        user_id = uuid.uuid4()

        with structlog.testing.capture_logs() as logs:
            error = onboarding_conflict(
                ["consent"], gate="require_onboarded", user_id=user_id
            )

        assert error.status_code == 409
        (line,) = [entry for entry in logs if entry["event"] == "conflict"]
        assert line["code"] == ONBOARDING_REQUIRED
        assert line["reason"] == "require_onboarded"
        assert line["steps"] == ["consent"]
        assert line["user_id"] == str(user_id)


class TestEveryConflictIsLogged:
    """A guard, not an example.

    The value of this change is that a 409 in production is one grep, and that
    holds only while it is true of ALL of them. A new conflict raised without a
    log line would quietly restore the situation this fixed, and nothing else in
    the suite would notice.
    """

    def test_no_409_is_raised_without_a_conflict_log(self):
        offenders = [
            path.relative_to(Path("app").parent)
            for path in Path("app").rglob("*.py")
            if re.search(r"HTTP_409_CONFLICT", path.read_text())
            and "log_conflict" not in path.read_text()
        ]
        assert offenders == [], (
            f"these raise a 409 without logging it: {offenders}. "
            "Call app.services.conflicts.log_conflict first (code + reason, no PII)."
        )
