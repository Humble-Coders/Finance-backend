"""A 422 names the field and never quotes the value.

FastAPI's default validation handler returns Pydantic's errors verbatim, and
each carries an `input` key holding the value that failed. For most APIs that is
a convenience. For this one it means a malformed statement import sends the
statement back in the response body — and from there into logs, error trackers
and anywhere else a 4xx travels. In the one endpoint whose entire purpose is
that we do not keep statements, that would be the whole promise undone by a
default.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient

from app.main import _validation_error
from app.schemas.statements import StatementParseIn

SECRET = "2026-08-14  TIM HORTONS #4821   12.40"


@pytest.fixture
def client():
    """The real handler, on a route using the real request schema."""
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, _validation_error)

    @app.post("/parse")
    async def parse(body: StatementParseIn):  # pragma: no cover - never valid here
        return {"ok": True}

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_the_statement_text_never_comes_back_in_a_422(client):
    async with client as api:
        response = await api.post(
            "/parse", json={"source_kind": "nonsense", "text": SECRET}
        )

    assert response.status_code == 422
    assert SECRET not in response.text
    assert "TIM HORTONS" not in response.text


@pytest.mark.asyncio
async def test_it_still_says_which_field_was_wrong(client):
    """Stripping the value must not strip the diagnosis — a 422 nobody can act
    on is its own failure."""
    async with client as api:
        response = await api.post(
            "/parse", json={"source_kind": "nonsense", "text": SECRET}
        )

    detail = response.json()["detail"]
    assert any("source_kind" in error["loc"] for error in detail)
    assert all("input" not in error for error in detail)
