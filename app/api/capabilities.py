"""GET /capabilities — what this household may see and do (PRD §4.6).

Clients render their UI from this payload. It is the ONLY thing that decides
what a user sees; there are no per-country branches in client code.

The payload controls what is SHOWN. Every gated endpoint must independently
re-check via `require_feature` for what is ALLOWED — a hidden feature is not a
secured feature.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_household
from app.db import get_session
from app.models.identity import Household
from app.schemas.capabilities import Capabilities
from app.services.capabilities import resolve

router = APIRouter(tags=["capabilities"])


@router.get("/capabilities", response_model=Capabilities)
async def get_capabilities(
    household: Household = Depends(current_household),
    session: AsyncSession = Depends(get_session),
) -> Capabilities:
    return await resolve(session, household)
