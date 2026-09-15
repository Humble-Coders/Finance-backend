"""The admin client's headers, and its refusals. Pure: nothing reaches Supabase."""

from __future__ import annotations

import pytest

from app.services.supabase_admin import SupabaseAdmin, SupabaseAdminError, admin_headers


def test_a_secret_key_goes_in_apikey_alone():
    # Not a JWT: sent as a Bearer token too, the gateway rejects it.
    assert admin_headers("sb_secret_example") == {"apikey": "sb_secret_example"}


def test_a_legacy_service_role_key_is_sent_both_ways():
    key = "eyJhbGciOiJIUzI1NiJ9.e30.signature"
    assert admin_headers(key) == {"apikey": key, "Authorization": f"Bearer {key}"}


@pytest.mark.asyncio
async def test_no_key_is_refused_before_any_request():
    with pytest.raises(SupabaseAdminError, match="not configured"):
        await SupabaseAdmin("https://example.supabase.co", "").delete_auth_user(
            "00000000-0000-0000-0000-000000000000"
        )


@pytest.mark.asyncio
async def test_an_id_that_is_not_a_uuid_never_reaches_the_url():
    with pytest.raises(SupabaseAdminError, match="not a UUID"):
        await SupabaseAdmin(
            "https://example.supabase.co", "sb_secret_x"
        ).delete_auth_user("../users")
