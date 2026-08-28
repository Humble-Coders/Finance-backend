"""Liveness and readiness endpoints.

/healthz  — process is up. Render's health check points here; it must never
            depend on the database, or a brief DB blip cycles the service.
/readyz   — dependencies reachable. For dashboards and deploy verification.
"""

import structlog
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.dsn import describe_dsn
from app.db import get_session

log = structlog.get_logger()

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """Prove the database is actually reachable.

    On failure this reports the exception type and the credential-free DSN,
    because the alternative — a bare 500 — means reading deploy logs to learn
    anything at all. Nothing secret is exposed: describe_dsn strips the user
    and password, and only the exception class name is returned, never its
    message (which can carry connection details).
    """
    dsn = describe_dsn(settings.database_dsn)
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        log.exception("readyz_database_unreachable", dsn=dsn)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "error",
            "database": "unreachable",
            "error": type(exc).__name__,
            "dsn": dsn,
        }

    return {"status": "ok", "database": "ok", "dsn": dsn}
