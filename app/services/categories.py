"""Categories a household makes for itself.

The seeded taxonomy is shared by everyone and never added to at runtime. When a
correction needs a category it lacks — "Kids' activities", "Side business" —
the household creates its own, visible to it alone.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["SLUG_MAX", "slug_for"]

# The column is String(64).
SLUG_MAX = 64

_NOT_SLUG = re.compile(r"[^a-z0-9]+")


def slug_for(name: str) -> str | None:
    """The slug a category name is known by, or None if it has none.

    Underscores, because that is how the seeded taxonomy spells them
    (`debt_payment`). The convention is load-bearing, not cosmetic: the check
    against system categories compares slugs, so a household's "Debt payment"
    has to come out as `debt_payment` to be recognised as the one that already
    exists. Hyphens would make it `debt-payment`, a different slug, and put two
    identical choices in the picker.

    Accents fold to their base letter rather than being dropped, so "Café" is
    `cafe` and not `caf`. Anything else that is not a letter or digit becomes a
    single underscore. A name made only of emoji or punctuation has no slug at
    all, and is refused rather than given an arbitrary one — two such
    categories would otherwise collide on whatever placeholder was chosen.

    Cut at a word boundary where it can be, so a long name loses a whole
    trailing word instead of ending mid-word.
    """
    folded = (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    slug = _NOT_SLUG.sub("_", folded).strip("_")
    if len(slug) > SLUG_MAX:
        cut = slug[:SLUG_MAX]
        slug = cut.rsplit("_", 1)[0] if "_" in cut else cut
    return slug or None
