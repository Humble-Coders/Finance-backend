"""Identity resolution — the logic that stops one person becoming two households.

Runs against a real Postgres because the guarantees are database guarantees:
the unique phone, the unique provider identity, and ON CONFLICT behaviour under
concurrency. Each test rolls back.
"""

from __future__ import annotations

import uuid

import pytest

from app.auth import AuthenticatedUser
from app.models.enums import AuthProvider
from app.services.identity import (
    PhoneAlreadyLinkedError,
    provider_from_claims,
    resolve_user,
)
from tests.conftest import requires_db

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]


def caller(
    *,
    sub: str | None = None,
    provider: str = "phone",
    email: str | None = None,
    phone: str | None = None,
    full_name: str | None = None,
) -> AuthenticatedUser:
    claims: dict = {"app_metadata": {"provider": provider}}
    if full_name:
        claims["user_metadata"] = {"full_name": full_name}
    return AuthenticatedUser(
        user_id=sub or str(uuid.uuid4()),
        email=email,
        phone=phone,
        claims=claims,
    )


class TestProviderFromClaims:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("google", AuthProvider.google),
            ("apple", AuthProvider.apple),
            ("phone", AuthProvider.phone),
            (None, AuthProvider.phone),
            ("something-new", AuthProvider.phone),
        ],
    )
    async def test_maps_supabase_provider(self, raw, expected):
        claims = {"app_metadata": {"provider": raw}} if raw else {}
        assert provider_from_claims(claims) is expected


class TestFirstSignIn:
    async def test_creates_one_user_and_one_household(self, db_session):
        resolved = await resolve_user(db_session, caller(phone="+14165550001"))
        assert resolved.created is True
        assert resolved.user.household_id == resolved.household.id

    async def test_country_code_is_left_null(self, db_session):
        """Never guessed — the region comes from the phone in ticket 2.1."""
        resolved = await resolve_user(db_session, caller(phone="+14165550002"))
        assert resolved.household.country_code is None

    async def test_a_google_user_without_a_phone_still_gets_a_household(
        self, db_session
    ):
        resolved = await resolve_user(
            db_session, caller(provider="google", email="a@example.com")
        )
        assert resolved.created is True
        assert resolved.user.phone is None


class TestIdempotency:
    async def test_repeat_calls_return_the_same_household(self, db_session):
        c = caller(phone="+14165550003")
        first = await resolve_user(db_session, c)
        second = await resolve_user(db_session, c)

        assert second.created is False
        assert second.user.id == first.user.id
        assert second.household.id == first.household.id


class TestLinkingAcrossProviders:
    async def test_a_matching_phone_links_instead_of_duplicating(self, db_session):
        """The path that prevents split financial history."""
        first = await resolve_user(
            db_session, caller(provider="phone", phone="+14165550004")
        )
        second = await resolve_user(
            db_session,
            caller(provider="google", phone="+14165550004", email="g@example.com"),
        )

        assert second.created is False
        assert second.user.id == first.user.id
        assert second.household.id == first.household.id

    async def test_a_matching_email_links_too(self, db_session):
        first = await resolve_user(
            db_session, caller(provider="google", email="same@example.com")
        )
        second = await resolve_user(
            db_session, caller(provider="apple", email="same@example.com")
        )
        assert second.user.id == first.user.id

    async def test_an_apple_relay_address_cannot_link_and_creates_a_new_person(
        self, db_session
    ):
        """Documents the limitation the phone requirement exists to cover.

        Hide My Email matches nothing, so without a phone these are two people
        as far as the system can tell. This is not a bug to fix here — it is why
        every signup route ends with a verified phone.
        """
        first = await resolve_user(
            db_session, caller(provider="google", email="real@example.com")
        )
        second = await resolve_user(
            db_session,
            caller(provider="apple", email="x7k2m@privaterelay.appleid.com"),
        )
        assert second.created is True
        assert second.user.id != first.user.id

    async def test_a_relay_address_is_stored_not_discarded(self, db_session):
        resolved = await resolve_user(
            db_session,
            caller(provider="apple", email="abc@privaterelay.appleid.com"),
        )
        assert resolved.user.email == "abc@privaterelay.appleid.com"


class TestAppleClaimsArriveOnlyOnce:
    async def test_a_later_sign_in_does_not_erase_email_or_name(self, db_session):
        """Apple sends email and name on the first authorization only.

        A naive "update from claims" would null them on the second sign-in and
        lose them permanently.
        """
        sub = str(uuid.uuid4())
        first = await resolve_user(
            db_session,
            caller(
                sub=sub,
                provider="apple",
                email="once@privaterelay.appleid.com",
                full_name="Ada Lovelace",
            ),
        )
        assert first.user.email == "once@privaterelay.appleid.com"
        assert first.user.display_name == "Ada Lovelace"

        second = await resolve_user(
            db_session, caller(sub=sub, provider="apple", email=None, full_name=None)
        )
        assert second.user.email == "once@privaterelay.appleid.com"
        assert second.user.display_name == "Ada Lovelace"


class TestPhoneConflict:
    async def test_claiming_someone_elses_phone_raises(self, db_session):
        await resolve_user(db_session, caller(phone="+14165550005"))

        other_sub = str(uuid.uuid4())
        await resolve_user(
            db_session, caller(sub=other_sub, provider="google", email="o@example.com")
        )

        with pytest.raises(PhoneAlreadyLinkedError):
            await resolve_user(
                db_session,
                caller(sub=other_sub, provider="google", phone="+14165550005"),
            )

    async def test_completing_your_own_phone_step_succeeds(self, db_session):
        """The normal Google-then-phone flow must not trip the conflict check."""
        sub = str(uuid.uuid4())
        await resolve_user(
            db_session, caller(sub=sub, provider="google", email="own@example.com")
        )
        resolved = await resolve_user(
            db_session,
            caller(
                sub=sub,
                provider="google",
                email="own@example.com",
                phone="+14165550006",
            ),
        )
        assert resolved.user.phone == "+14165550006"
