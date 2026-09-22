"""Turning a bank's description into something two statements can agree on.

Banks print the same purchase differently. `TIM HORTONS #4821 OTTAWA ON` on one
statement is `POS PURCHASE TIM HORTONS 4821` on another, and a person looking at
both sees one coffee. The dedup key needs them to be one string.

Two outputs, for two audiences:

* `normalized(description)` is a **comparison key**. It is ugly, nobody ever
  sees it, and its only job is to be identical for the same purchase.
* `merchant(description)` is what a **person** reads, and what the categorizer
  is given. Title case, store numbers gone, still recognisable.

**Changing `normalized` changes the dedup key for every row ever written.**
Every existing `transaction.normalized_description` was computed by the version
of this function that existed at the time. Improve the regex without recomputing
them and the old rows keep their old keys: a re-import matches nothing, the
constraint rejects nothing, and the same statement lands twice with no error
anywhere.

So a change here ships with a migration that recomputes every stored value, and
`VERSION` below goes up. `test_normalization.py` fails until that happens — it
asserts every stored key equals what this function produces today, which is the
only way to make the requirement unignorable.
"""

from __future__ import annotations

import re

__all__ = ["VERSION", "normalized", "merchant"]

# Bump when the output changes for any input. See the module docstring: a bump
# without a backfill migration is a silent data bug.
VERSION = 1

# Noise the bank adds, not the merchant's name.
# Note what is NOT here: `interac`. "INTERAC E-TRANSFER FEE" is the name of the
# thing, not a prefix on something else, and stripping it leaves "e transfer
# fee" — which reads worse and categorizes worse. A prefix only belongs in this
# list when removing it still leaves the merchant behind.
_PREFIXES = re.compile(
    r"^(pos\s+purchase|point\s+of\s+sale|purchase|debit|credit|payment|"
    r"visa\s+debit|misc\s+payment|preauthorized|preauth)\b\s*",
    re.IGNORECASE,
)
# A store, terminal or reference number: `#4821`, `*2R41K`, a masked account.
_REFERENCES = re.compile(r"[#*•]+\s*[\w-]*\d[\w-]*")
# `2026-08-14`, `08/14`, `14 AUG` sitting inside a description.
_DATES = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?|"
    r"\d{1,2}\s?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*)\b",
    re.IGNORECASE,
)
# A bare run of digits left over once the markers are gone — `LOBLAWS 1042`.
_TRAILING_DIGITS = re.compile(r"\b\d{2,}\b")
_PUNCTUATION = re.compile(r"[^a-z0-9\s]")
_SPACE = re.compile(r"\s+")


def _strip_noise(description: str) -> str:
    text = _PREFIXES.sub("", description.strip())
    text = _REFERENCES.sub(" ", text)
    text = _DATES.sub(" ", text)
    text = _TRAILING_DIGITS.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def normalized(description: str | None) -> str:
    """The comparison key. Same purchase in, same string out.

    Empty in, empty out — and the caller must not treat that as a match key on
    its own. A description we could not read is not evidence that two rows are
    the same transaction.
    """
    if not description:
        return ""
    text = _strip_noise(description).lower()
    text = _PUNCTUATION.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def merchant(description: str | None) -> str | None:
    """What a person reads: `Tim Hortons`, not `tim hortons` or the raw line.

    This is also what the categorizer receives, which is why it is derived from
    the cleaned form rather than the original: the reference numbers stripped
    above are exactly the kind of detail the PRD keeps out of a model prompt
    (Appendix A.3), and they help it not at all.
    """
    if not description:
        return None
    text = _strip_noise(description)
    if not text:
        return None
    # Hyphenated names are one word to `capitalize()`, which gives
    # "Petro-canada". Capitalize each part instead.
    words = [
        "-".join(part.capitalize() for part in word.split("-"))
        if word.isupper()
        else word
        for word in text.split()
    ]
    return " ".join(words)[:255]
