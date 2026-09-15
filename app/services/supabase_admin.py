"""Supabase Auth admin operations — the only code that uses the service_role key.

That key bypasses every Row Level Security policy and can act as any user, so
it lives in this service's environment and nowhere else (CLAUDE.md). This module
is deliberately tiny: each operation it offers is one more thing the key can be
used for, and each should earn its place.

Today there is one: deleting an **orphan** — the empty sign-in account that a
new method creates before the person proves, at the phone step, that they
already have an account. Supabase keeps the provider identity attached to that
orphan until it is deleted, and while it is attached the client cannot link
that identity to the real account.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx

from app.config import get_settings

__all__ = ["SupabaseAdmin", "SupabaseAdminError", "get_supabase_admin"]

_TIMEOUT_SECONDS = 10.0


class SupabaseAdminError(Exception):
    """An admin call that did not complete.

    Messages carry status codes and exception types only, never a response
    body: Supabase's user responses include the person's email and phone.
    """


@dataclass(frozen=True)
class SupabaseAdmin:
    base_url: str
    service_role_key: str

    async def delete_auth_user(self, auth_user_id: str) -> None:
        """Delete a Supabase Auth user. Idempotent: an already-deleted one is success.

        Idempotency is what makes the link flow safe to retry. The database side
        is removed before this is called, so a retry after a failed delete finds
        nothing left to clean up locally and simply tries this again.
        """
        if not self.service_role_key:
            raise SupabaseAdminError("SUPABASE_SERVICE_ROLE_KEY is not configured")
        try:
            # It goes into a URL path; refuse anything that is not a plain UUID.
            user_id = uuid.UUID(auth_user_id)
        except ValueError as exc:
            raise SupabaseAdminError("auth user id is not a UUID") from exc

        url = f"{self.base_url.rstrip('/')}/auth/v1/admin/users/{user_id}"
        headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                response = await client.delete(url, headers=headers)
        except httpx.HTTPError as exc:
            raise SupabaseAdminError(f"network failure: {type(exc).__name__}") from exc

        if response.status_code == 404:
            return
        if response.status_code >= 400:
            raise SupabaseAdminError(f"supabase admin returned {response.status_code}")


def get_supabase_admin() -> SupabaseAdmin:
    """FastAPI dependency. Overridden in tests, which must never reach Supabase."""
    settings = get_settings()
    return SupabaseAdmin(
        base_url=settings.supabase_url,
        service_role_key=settings.supabase_service_role_key,
    )
