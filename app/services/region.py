"""Region from a verified phone number (PRD §4.6).

Resolved here, server-side, and never in a client: one implementation, and a
modified app cannot choose its own region.

libphonenumber, not an area-code list. ``+1`` is the whole North American
Numbering Plan — Canada, the US, most of the Caribbean and several US
territories — and a hand-maintained table of which area code belongs where is
wrong as soon as a new overlay is issued. Google keeps that metadata current.

A number that cannot be placed returns ``None``. Nothing here ever guesses: a
wrong region shows someone the wrong tax accounts, the wrong currency and the
wrong disclaimer, which is worse than asking them.
"""

from __future__ import annotations

import phonenumbers

__all__ = ["KNOWN_REGIONS", "normalize_region", "region_for_phone"]

# Every region libphonenumber has numbering metadata for: ISO 3166-1 alpha-2
# codes only. Non-geographic numbering (satellite phones, +800 freephone) is
# reported as "001" and is deliberately not in here — it names no country.
KNOWN_REGIONS: frozenset[str] = frozenset(phonenumbers.SUPPORTED_REGIONS)


def region_for_phone(phone: str | None) -> str | None:
    """The region that issued ``phone``, or ``None`` if it cannot be placed.

    Accepts the number with or without its leading ``+``. Supabase strips the
    ``+`` when it stores a number, so real tokens carry ``14165550100`` while
    hand-written fixtures tend to say ``+14165550100``; both mean the same
    number and must give the same answer.
    """
    if not phone:
        return None
    candidate = phone.strip().replace(" ", "")
    if not candidate.startswith("+"):
        candidate = "+" + candidate
    try:
        number = phonenumbers.parse(candidate, None)
    except phonenumbers.NumberParseException:
        return None
    region = phonenumbers.region_code_for_number(number)
    return region if region in KNOWN_REGIONS else None


def normalize_region(code: str | None) -> str | None:
    """``code`` as a known upper-case region, or ``None`` if it is not one."""
    if not code:
        return None
    candidate = code.strip().upper()
    return candidate if candidate in KNOWN_REGIONS else None
