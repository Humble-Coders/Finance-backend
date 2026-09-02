"""Resolving a verified token to a user and household.

The problem this solves is not authentication — `app.auth` already does that —
but **identity**. One person may sign in three ways (phone OTP, Google, Apple),
and each produces a different Supabase `sub`. Treating each as a new person
would give them multiple households and split their financial history, which
support cannot repair.

Email cannot be the linking key: Apple's *Hide My Email* returns a relay address
matching nothing else the person has used. **The verified phone number can**,
because every signup route ends with one (PRD §4.6) — which is the real reason
the phone step is mandatory.

A note on `user.auth_user_id`: it records the **first** Supabase account we saw
for a person. When a second provider is linked by phone, that account's `sub`
lives only in `user_identity`. All resolution therefore goes through
`user_identity` — never through `auth_user_id`, which would silently fail for
anyone using more than one provider.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser
from app.models.enums import AuthProvider
from app.models.identity import Household, User, UserIdentity

__all__ = [
    "PhoneAlreadyLinkedError",
    "ResolvedIdentity",
    "provider_from_claims",
    "resolve_user",
]


class PhoneAlreadyLinkedError(Exception):
    """The caller's verified phone already belongs to a different user.

    Raised rather than resolved automatically: merging two people's financial
    records is not something to do on a guess, and silently creating a duplicate
    is the outcome this whole module exists to prevent.
    """

    def __init__(self, phone: str) -> None:
        super().__init__("phone already linked to another user")
        self.phone = phone


@dataclass(frozen=True)
class ResolvedIdentity:
    user: User
    household: Household
    created: bool


def provider_from_claims(claims: dict) -> AuthProvider:
    """Which provider issued this session.

    Supabase records it in `app_metadata.provider`. Anything unrecognised is
    treated as the phone route, which is the primary path — a wrong guess here
    only mislabels an identity row, it cannot merge two people.
    """
    raw = (claims.get("app_metadata") or {}).get("provider")
    match raw:
        case "google":
            return AuthProvider.google
        case "apple":
            return AuthProvider.apple
        case _:
            return AuthProvider.phone


async def _by_provider_identity(
    session: AsyncSession, provider: AuthProvider, provider_user_id: str
) -> User | None:
    result = await session.execute(
        select(User)
        .join(UserIdentity, UserIdentity.user_id == User.id)
        .where(
            UserIdentity.provider == provider,
            UserIdentity.provider_user_id == provider_user_id,
        )
    )
    return result.scalar_one_or_none()


async def _by_phone(session: AsyncSession, phone: str) -> User | None:
    result = await session.execute(select(User).where(User.phone == phone))
    return result.scalar_one_or_none()


async def _by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalars().first()


async def _link_identity(
    session: AsyncSession,
    user: User,
    provider: AuthProvider,
    provider_user_id: str,
) -> None:
    """Attach a provider identity, tolerating a concurrent insert.

    ON CONFLICT DO NOTHING rather than check-then-insert: two first requests for
    the same new user arrive in parallel often enough to matter, and the loser of
    that race must not raise.
    """
    await session.execute(
        pg_insert(UserIdentity)
        .values(user_id=user.id, provider=provider, provider_user_id=provider_user_id)
        # Inferred from the columns rather than named: the constraint is called
        # `provider_identity`, not what the naming convention would produce, and
        # a wrong name here fails only at runtime.
        .on_conflict_do_nothing(index_elements=["provider", "provider_user_id"])
    )


def _absorb_claims(user: User, caller: AuthenticatedUser) -> None:
    """Fill in what the token knows, without ever erasing what it does not.

    Apple returns the email and name **only on the first authorization**; every
    later sign-in omits them. A naive assignment would overwrite stored values
    with nulls on the second sign-in and lose them permanently. So each field is
    only ever filled, never cleared.

    A `…@privaterelay.appleid.com` address is a real, deliverable address and is
    stored like any other.
    """
    if caller.email and not user.email:
        user.email = caller.email
    if caller.phone and not user.phone:
        user.phone = caller.phone

    name = (caller.claims.get("user_metadata") or {}).get("full_name")
    if name and not user.display_name:
        user.display_name = name


async def resolve_user(
    session: AsyncSession, caller: AuthenticatedUser
) -> ResolvedIdentity:
    """Turn a verified token into exactly one user and household.

    Resolution order (PRD §4.6). The order is the point: each step is a weaker
    signal than the last, and stopping early is what prevents duplicates.

        1. this provider identity is already known
        2. the verified phone matches an existing user  <- prevents duplicates
        3. the verified email matches                    <- fails for Apple relay
        4. otherwise, a new person
    """
    provider = provider_from_claims(caller.claims)
    provider_user_id = caller.user_id

    # 1. Known identity.
    user = await _by_provider_identity(session, provider, provider_user_id)
    if user is not None:
        # Check before mutating. Absorbing first would leave a pending UPDATE
        # that autoflushes during the lookup below, so Postgres raises a raw
        # IntegrityError before the friendly, client-actionable error can.
        await _assert_phone_not_taken(session, user, caller)
        _absorb_claims(user, caller)
        await session.flush()
        household = await session.get(Household, user.household_id)
        return ResolvedIdentity(user=user, household=household, created=False)

    # 2/3. A person we already know, arriving via a new provider.
    linked: User | None = None
    if caller.phone:
        linked = await _by_phone(session, caller.phone)
    if linked is None and caller.email:
        linked = await _by_email(session, caller.email)

    if linked is not None:
        await _link_identity(session, linked, provider, provider_user_id)
        _absorb_claims(linked, caller)
        await session.flush()
        household = await session.get(Household, linked.household_id)
        return ResolvedIdentity(user=linked, household=household, created=False)

    # 4. Someone new.
    household = Household(country_code=None)  # never guessed; see PRD §4.6
    session.add(household)
    await session.flush()

    user = User(
        household_id=household.id,
        auth_user_id=provider_user_id,
        email=caller.email,
        phone=caller.phone,
        display_name=(caller.claims.get("user_metadata") or {}).get("full_name"),
    )
    session.add(user)
    await session.flush()

    await _link_identity(session, user, provider, provider_user_id)
    await session.flush()
    return ResolvedIdentity(user=user, household=household, created=True)


async def _assert_phone_not_taken(
    session: AsyncSession, user: User, caller: AuthenticatedUser
) -> None:
    """A phone arriving on the token must not already belong to someone else.

    This is the moment a Google user completes the phone step: the token now
    carries a number the user row lacks. If it belongs to another account, that
    is two real people or one person with a duplicate — either way it needs a
    human, not a guess.
    """
    if not caller.phone or user.phone == caller.phone:
        return
    owner = await _by_phone(session, caller.phone)
    if owner is not None and owner.id != user.id:
        raise PhoneAlreadyLinkedError(caller.phone)
