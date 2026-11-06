"""Deleting an account: everything the person has here, and their sign-in.

PRD Appendix A.5: a hard delete. The household's rows go with it through the
database's own ON DELETE CASCADE — every table hangs off `household` or
`user` — so nothing here lists tables, and a table added later cannot be
missed. Consent records go too (`consent_event` cascades from `user`, by
design). A household someone else still belongs to keeps its rows; only this
person's own are removed.

The sign-in is Supabase's, removed with the service-role key, which only the
server holds. It is asked for *before* the database commits, so a failure
there leaves everything as it was and the person can try again; the one gap
— the sign-in gone and the commit then failing — leaves rows nobody can sign
in to, which is logged loudly.
"""

from __future__ import annotations

import httpx
import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.identity import Household, User

log = structlog.get_logger()

__all__ = ["SignInDeletionError", "delete_account"]

TIMEOUT_SECONDS = 15.0


class SignInDeletionError(Exception):
    """Supabase did not remove the sign-in; nothing was deleted."""


async def delete_sign_in(settings: Settings, auth_user_id: str) -> None:
    """Remove the Supabase user. Already gone counts as done."""
    if not settings.supabase_service_role_key:
        raise SignInDeletionError("no service-role key configured")
    url = f"{settings.supabase_url.rstrip('/')}/auth/v1/admin/users/{auth_user_id}"
    headers = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            response = await http.delete(url, headers=headers)
    except httpx.HTTPError as error:
        raise SignInDeletionError(type(error).__name__) from error
    if response.status_code == 404:
        return
    if response.status_code >= 400:
        raise SignInDeletionError(f"supabase answered {response.status_code}")


async def delete_account(
    session: AsyncSession, settings: Settings, user: User, household: Household
) -> None:
    """Remove the person, their household when no one else is in it, and
    their sign-in, in that order of asking; commit only once all agreed."""
    others = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(User.household_id == household.id, User.id != user.id)
    )
    auth_user_id = user.auth_user_id
    if others:
        await session.execute(delete(User).where(User.id == user.id))
    else:
        await session.execute(delete(Household).where(Household.id == household.id))
    await session.flush()
    try:
        await delete_sign_in(settings, auth_user_id)
    except SignInDeletionError:
        await session.rollback()
        raise
    try:
        await session.commit()
    except Exception:
        log.error(
            "account_deletion_commit_failed_after_sign_in_removed",
            household_id=str(household.id),
        )
        raise
    log.info("account_deleted", household_kept=bool(others))
