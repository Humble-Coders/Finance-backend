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

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser
from app.models.enums import AuthProvider
from app.models.identity import Household, User, UserIdentity

# Constraint names from the core schema migration. Branching on the name is what
# lets a concurrent failure be answered correctly: the two constraints below mean
# entirely different things, and `IntegrityError` alone does not distinguish them.
AUTH_USER_ID_UNIQUE = "uq_user_auth_user_id"
PHONE_UNIQUE = "uq_user_phone"

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


async def _flush_translating_conflicts(
    session: AsyncSession, caller: AuthenticatedUser
) -> None:
    """Flush, turning a phone collision into the error the client understands.

    **Every write that can set `user.phone` must go through here.** The checks
    that precede those writes are check-then-act: another transaction can claim
    the number between the SELECT and the flush, and then `uq_user_phone` raises
    a raw IntegrityError. Without translation that surfaces as a 500, where the
    same situation without a race correctly returns 409.

    This exists as one helper rather than three copies because the same bug was
    fixed twice in different places before anyone noticed it was one bug: the
    fixes landed where the problem was found, not where the class of problem
    lives. Anything that touches a phone number should inherit this, not
    reimplement it.
    """
    try:
        await session.flush()
    except IntegrityError as exc:
        constraint = _violated_constraint(exc)
        await session.rollback()
        if constraint == PHONE_UNIQUE:
            raise PhoneAlreadyLinkedError(caller.phone or "") from exc
        raise


def _violated_constraint(error: IntegrityError) -> str | None:
    """Which unique constraint an IntegrityError broke, if we can tell.

    asyncpg carries `constraint_name`, but SQLAlchemy's wrapping does not always
    preserve it, so fall back to the message — which always names it.
    """
    original = getattr(error, "orig", None)
    for candidate in (original, getattr(original, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    match = re.search(r'unique constraint "([^"]+)"', str(error))
    return match.group(1) if match else None


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
    """Oldest match wins.

    `user.email` is indexed but **not unique** — two rows sharing one should be
    impossible, since this very lookup prevents it, but "impossible" plus an
    arbitrary pick is a bad combination for something that decides whose
    financial records a sign-in attaches to. Ordering makes it deterministic.
    """
    result = await session.execute(
        select(User).where(User.email == email).order_by(User.created_at.asc())
    )
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
        # The check above is check-then-act; the flush is where a concurrent
        # claim on the same number actually surfaces.
        await _flush_translating_conflicts(session, caller)
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
        await _flush_translating_conflicts(session, caller)
        household = await session.get(Household, linked.household_id)
        return ResolvedIdentity(user=linked, household=household, created=False)

    # 4. Someone new.
    try:
        return await _create(session, caller, provider, provider_user_id)
    except IntegrityError as exc:
        # Two inserts collided. WHICH constraint broke decides the answer, and
        # IntegrityError alone does not say — so read it before reacting.
        constraint = _violated_constraint(exc)

        # Rolls back the caller's transaction. The service does not own this
        # session, but there is no other way to continue after a failed flush;
        # `current_identity` commits afterwards, and the test fixture's savepoint
        # mode tolerates it.
        await session.rollback()

        if constraint == PHONE_UNIQUE:
            # A DIFFERENT person already holds this number — same meaning as the
            # sequential path, so the same answer. (Kept here as well as in
            # _flush_translating_conflicts because this path must also decide
            # whether to retry, which the helper cannot know.)
            raise PhoneAlreadyLinkedError(caller.phone or "") from exc

        if constraint is not None and constraint != AUTH_USER_ID_UNIQUE:
            # Some other constraint we have no recovery for. Raising beats
            # retrying blindly and reporting a misleading outcome.
            raise

        # Same account, two simultaneous first requests. `auth_user_id` is
        # UNIQUE, which is what protected the data; the winner has committed, so
        # resolving again finds their rows. Retried exactly once — a second
        # failure is not a race.

    user = await _by_provider_identity(session, provider, provider_user_id)
    if user is None:
        # The winner created the user but its identity row is not visible, so
        # fall back to the unique column the race was actually decided on.
        result = await session.execute(
            select(User).where(User.auth_user_id == provider_user_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            # Neither lookup found the winner. Not a race we understand, so fail
            # loudly rather than inventing a second household for this person.
            raise RuntimeError(
                "identity resolution retried after an integrity error but found "
                f"no user for provider_user_id={provider_user_id!r}"
            )
        await _link_identity(session, user, provider, provider_user_id)
        await session.flush()

    household = await session.get(Household, user.household_id)
    return ResolvedIdentity(user=user, household=household, created=False)


async def _create(
    session: AsyncSession,
    caller: AuthenticatedUser,
    provider: AuthProvider,
    provider_user_id: str,
) -> ResolvedIdentity:
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
