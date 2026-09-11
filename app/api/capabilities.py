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

from app.api.deps import current_identity
from app.db import get_session
from app.schemas.capabilities import Capabilities
from app.services.capabilities import resolve
from app.services.identity import ResolvedIdentity
from app.services.onboarding import onboarding_state

router = APIRouter(tags=["capabilities"])


@router.get("/capabilities", response_model=Capabilities)
async def get_capabilities(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> Capabilities:
    payload = await resolve(session, identity.household)
    # The same rule /me uses (app/services/onboarding.py). The two endpoints once
    # decided separately and disagreed; they must not be able to again.
    state = await onboarding_state(session, identity.user, identity.household)
    payload.onboarding_required = state.steps
    return payload
