"""Shared FastAPI dependencies.

`current_household` is the one every later endpoint depends on: it turns a
verified token into the household whose data the caller may see. Household
scoping is the boundary that keeps one person's finances out of another's, so
endpoints should resolve through here rather than reading `household_id` from
anything the client sent.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, current_user
from app.db import get_session
from app.models.identity import Household, User
from app.services.identity import (
    PhoneAlreadyLinkedError,
    ResolvedIdentity,
    resolve_user,
)

__all__ = ["current_identity", "current_household", "current_db_user"]

PHONE_ALREADY_LINKED = "phone_already_linked"


async def current_identity(
    caller: AuthenticatedUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ResolvedIdentity:
    """Resolve the caller to their user and household, creating them if new.

    Commits, because resolution may create rows — a caller should not have to
    know whether they were the first request for this account.
    """
    try:
        resolved = await resolve_user(session, caller)
    except PhoneAlreadyLinkedError as exc:
        await session.rollback()
        # 409: the request is well-formed, but reconciling two accounts is a
        # decision for a person. Coded so the client can act on it rather than
        # parsing prose.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": PHONE_ALREADY_LINKED,
                "message": (
                    "That phone number is already linked to another account. "
                    "Sign in with the original method, or contact support."
                ),
            },
        ) from exc

    await session.commit()
    return resolved


async def current_household(
    identity: ResolvedIdentity = Depends(current_identity),
) -> Household:
    return identity.household


async def current_db_user(
    identity: ResolvedIdentity = Depends(current_identity),
) -> User:
    return identity.user
