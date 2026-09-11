"""GET /legal/terms — the account terms a user is asked to accept at signup."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, current_user
from app.db import get_session
from app.schemas.legal import TermsOut
from app.services.onboarding import current_terms

router = APIRouter(tags=["legal"])

NO_TERMS = "no_terms"


@router.get("/legal/terms", response_model=TermsOut)
async def read_terms(
    _caller: AuthenticatedUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TermsOut:
    """The terms in force, for the consent screen.

    Signed-in callers only, but depends on `current_user` rather than
    `current_identity`: reading the terms creates nothing.
    """
    terms = await current_terms(session)
    if terms is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": NO_TERMS, "message": "No account terms are configured."},
        )
    return TermsOut(
        version=terms.version, body=terms.body, effective_from=terms.effective_from
    )
