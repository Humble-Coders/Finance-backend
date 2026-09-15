"""Telling a person that something changed on their account.

**Delivery is not wired yet.** This service has no email, SMS or push sender,
and choosing one means a vendor and credentials. Until one exists, alerts are
written to the log — enough to prove the hook fires, not enough to protect
anyone.

**Wire a real sender before launch.** This alert is the defence against a
reassigned email address: a work mailbox handed to a new hire, or a lapsed
domain someone else bought, can be used to add a sign-in method to the previous
owner's account. Telling the account "Google was added — not you?" is what lets
the real owner notice.
"""

from __future__ import annotations

import uuid

import structlog

from app.models.enums import AuthProvider

__all__ = ["sign_in_method_added"]

log = structlog.get_logger()


def sign_in_method_added(user_id: uuid.UUID, provider: AuthProvider) -> None:
    """A sign-in method was attached to an account that already had one."""
    # Identifiers only. The address a real notification would be sent to is
    # personal data and must not reach the log (CLAUDE.md → Privacy).
    log.info(
        "sign_in_method_added",
        user_id=str(user_id),
        provider=provider.value,
        delivered=False,
    )
