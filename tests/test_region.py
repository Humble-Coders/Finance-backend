"""Region from a phone number (#24) — pure, no database.

The matrix is libphonenumber's real answers, recorded rather than assumed: `+1`
alone covers Canada, the US and much of the Caribbean, so the whole point of
the library is that these are not all "CA".
"""

from __future__ import annotations

import pytest

from app.services.region import KNOWN_REGIONS, normalize_region, region_for_phone


@pytest.mark.parametrize(
    ("phone", "region"),
    [
        ("+14165551234", "CA"),  # Toronto
        ("+12125551234", "US"),  # New York — same +1 as Canada
        ("+18765551234", "JM"),  # Jamaica, also +1
        ("+17875551234", "PR"),  # Puerto Rico, also +1
        ("+442071234567", "GB"),
        ("+919876543210", "IN"),
        ("+61291234567", "AU"),
    ],
)
def test_places_a_number_in_its_region(phone, region):
    assert region_for_phone(phone) == region


def test_the_plus_is_optional():
    """Supabase stores and signs numbers without the '+'; fixtures often add it."""
    assert region_for_phone("14165551234") == region_for_phone("+14165551234") == "CA"


def test_the_supabase_test_number_is_placed_in_canada():
    """Our test sign-in number is a fictional 555-01xx one. libphonenumber still
    places it, so test sign-ups get CA rather than the "pick a region" step."""
    assert region_for_phone("14165550100") == "CA"


@pytest.mark.parametrize(
    "phone",
    [None, "", "   ", "garbage", "+1", "+999123", "+80012345678"],
)
def test_never_guesses(phone):
    """Unparseable, incomplete, unassigned, or non-geographic (+800 freephone
    belongs to no country) — all None, never a best guess."""
    assert region_for_phone(phone) is None


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("CA", "CA"),
        ("ca", "CA"),
        (" gb ", "GB"),
        ("ZZ", None),
        ("CAN", None),
        ("001", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_region(code, expected):
    assert normalize_region(code) == expected


def test_known_regions_are_iso_alpha_2():
    assert KNOWN_REGIONS and all(len(r) == 2 and r.isupper() for r in KNOWN_REGIONS)
