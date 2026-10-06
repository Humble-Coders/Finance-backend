"""Database errors never carry the values a query was given (CLAUDE.md: no
PII in logs).

Any traceback the service logs — an unhandled 500, or a dashboard section
that failed — includes the error's text, and SQLAlchemy appends the
statement's parameters to it unless the engine hides them.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.conftest import requires_db

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

MARKER = "household-amount-7f3a9c"


async def test_a_failed_statement_s_error_omits_its_values(db_session):
    with pytest.raises(DBAPIError) as raised:
        async with db_session.begin_nested():
            # Division by zero: Postgres's own message does not echo the
            # marker, so if it appears it came from the parameters.
            await db_session.execute(
                text("SELECT CAST(:marker AS text), 1 / :zero"),
                {"marker": MARKER, "zero": 0},
            )

    message = str(raised.value)
    assert MARKER not in message
    assert "parameters hidden" in message
    assert "division by zero" in message, "the cause is still there to debug"
