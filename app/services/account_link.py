"""Removing the empty account a new sign-in method created — the orphan.

When someone who already has an account signs in with a method it does not yet
know (Apple with Hide My Email, say), Supabase creates a new sign-in account and
`/me` gives it a user and household before anything can tell the two apart.
The phone step then finds the number is taken. The person signs into their real
account, and this removes the orphan so its sign-in method is free to be linked
there — Supabase will not link an identity still attached to another user.

Three properties make that safe:

- **Both sessions are proven.** The caller is signed in to the real account and
  also presents the orphan's token. Nobody can remove an account they do not
  hold.
- **An orphan is provably empty.** It has no verified phone and no row in any
  household table that holds financial data. Every endpoint that writes such
  data refuses until onboarding completes, and the phone is its first step, so
  an account that never verified one cannot have written any — but that is
  checked here, not assumed.
- **It is idempotent.** The database side is removed first. If the Supabase
  delete then fails, a retry finds nothing left locally and simply tries again.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 — every table must be registered in the metadata
from app.auth import AuthenticatedUser
from app.db import Base
from app.models.identity import Household, HouseholdRegionChange, User, UserIdentity
from app.models.platform import SubscriptionEntitlement
from app.services.identity import ResolvedIdentity
from app.services.supabase_admin import SupabaseAdmin, SupabaseAdminError

__all__ = [
    "LinkError",
    "LinkTargetIncomplete",
    "NothingToLink",
    "OrphanCleanupFailed",
    "OrphanNotEmpty",
    "absorb_orphan",
]


class LinkError(Exception):
    """Base for every refusal below."""


class LinkTargetIncomplete(LinkError):
    """The account being linked into has no verified phone yet."""


class NothingToLink(LinkError):
    """The "orphan" is the caller's own account."""


class OrphanNotEmpty(LinkError):
    """It holds a verified phone or financial data, so it is a real account."""


class OrphanCleanupFailed(LinkError):
    """The database side is done, but Supabase did not delete the sign-in account."""


# Household tables that hold no financial data, so an orphan may have rows in
# them and still count as empty. Named through the models rather than as
# strings, so a rename cannot quietly widen what counts as disposable.
#
# Every OTHER table with a `household_id` counts as data — including any added
# later. A new table therefore blocks orphan removal until someone decides it
# belongs here, which is the right default for financial records.
_NOT_FINANCIAL = frozenset(
    {
        User.__tablename__,
        HouseholdRegionChange.__tablename__,
        SubscriptionEntitlement.__tablename__,
    }
)


async def _orphan_user(session: AsyncSession, orphan: AuthenticatedUser) -> User | None:
    result = await session.execute(
        select(User)
        .outerjoin(UserIdentity, UserIdentity.user_id == User.id)
        .where(
            or_(
                User.auth_user_id == orphan.user_id,
                UserIdentity.provider_user_id == orphan.user_id,
            )
        )
        .limit(1)
    )
    return result.scalars().first()


async def _holds_financial_data(session: AsyncSession, household_id: uuid.UUID) -> bool:
    for table in Base.metadata.sorted_tables:
        if table.name in _NOT_FINANCIAL or "household_id" not in table.c:
            continue
        count = await session.execute(
            select(func.count())
            .select_from(table)
            .where(table.c.household_id == household_id)
        )
        if count.scalar_one():
            return True
    return False


async def absorb_orphan(
    session: AsyncSession,
    target: ResolvedIdentity,
    orphan: AuthenticatedUser,
    admin: SupabaseAdmin,
) -> None:
    """Delete the orphan's rows, then its Supabase sign-in account."""
    if target.user.phone is None:
        # Linking into an account that has not verified a phone would just move
        # the duplicate problem, not solve it.
        raise LinkTargetIncomplete()

    orphan_user = await _orphan_user(session, orphan)
    if orphan_user is not None:
        if orphan_user.id == target.user.id:
            raise NothingToLink()
        if (
            orphan_user.phone is not None
            or orphan_user.household_id == target.household.id
            or await _holds_financial_data(session, orphan_user.household_id)
        ):
            raise OrphanNotEmpty()
        # ON DELETE CASCADE takes the user, their sign-in identities, phone
        # history and consent records with the household.
        await session.execute(
            delete(Household).where(Household.id == orphan_user.household_id)
        )
        await session.commit()
    elif orphan.user_id == target.user.auth_user_id:
        raise NothingToLink()

    try:
        await admin.delete_auth_user(orphan.user_id)
    except SupabaseAdminError as exc:
        raise OrphanCleanupFailed() from exc
