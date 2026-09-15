"""Changes to an account worth a record, kept in the backend only.

**Nobody is notified** (manager decision, 2026-09-15): these are never emailed
or shown to the person. They exist so support and audits can see when a sign-in
method joined an account.

The durable record is the `user_identity` row itself — its `created_at` is when
the method was added. This log line adds the moment it happened in a request.
"""

from __future__ import annotations

import uuid

import structlog

from app.models.enums import AuthProvider

__all__ = ["sign_in_method_added"]

log = structlog.get_logger()


def sign_in_method_added(user_id: uuid.UUID, provider: AuthProvider) -> None:
    """A sign-in method was attached to an account that already had one."""
    # Identifiers only: the account's email and phone are personal data and
    # must not reach the log (CLAUDE.md → Privacy).
    log.info("sign_in_method_added", user_id=str(user_id), provider=provider.value)
