"""What counts as a verified email in a Supabase token.

Pure — no database, no network. Linking accounts by email trusts this function
alone, so every way a token can fail to say "verified" must read as unverified.
"""

from __future__ import annotations

import pytest

from app.auth import email_verified_from_claims


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({"user_metadata": {"email_verified": True}}, True),
        ({"email_verified": True}, True),
        ({"user_metadata": {"email_verified": False}}, False),
        ({"user_metadata": {"email_verified": "true"}}, False),
        ({"user_metadata": {"email_verified": 1}}, False),
        ({"user_metadata": {}}, False),
        ({"user_metadata": None}, False),
        ({"user_metadata": ["email_verified"]}, False),
        ({}, False),
    ],
)
def test_only_an_explicit_true_counts(claims, expected):
    assert email_verified_from_claims(claims) is expected
