"""Response shapes for legal copy."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

__all__ = ["TermsOut", "PolicyConsentIn", "ConsentAcceptedOut"]


class TermsOut(BaseModel):
    version: str
    body: str
    effective_from: datetime | None


class PolicyConsentIn(BaseModel):
    """The version the client actually displayed.

    Required, never defaulted to "whatever is current": the point of recording
    consent is knowing which words the person read.
    """

    version: str


class ConsentAcceptedOut(BaseModel):
    version: str


class ConsentStatusOut(BaseModel):
    """Where this person stands on AI processing, for the Settings row (#42).

    `version` is the policy in force — what consenting now would mean agreeing
    to — or null when none is configured.
    """

    consented: bool
    version: str | None
