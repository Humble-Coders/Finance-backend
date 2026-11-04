"""Split expense's arithmetic: every share in minor units, adding up exactly.

The same cases are the shared `SplitMathTest`'s, so the app's preview and the
server's saved shares cannot disagree.
"""

from __future__ import annotations

import pytest

from app.services.splits import equal_shares, item_shares


class TestEqually:
    def test_the_left_over_minor_units_go_to_the_first_people(self):
        assert equal_shares(1000, 3) == [334, 333, 333]

    def test_an_even_total_is_even(self):
        assert equal_shares(74550, 3) == [24850, 24850, 24850]

    def test_nobody_to_split_with_is_refused(self):
        with pytest.raises(ValueError):
            equal_shares(1000, 0)


class TestByItems:
    def test_each_item_to_its_people_and_tax_and_tip_by_what_each_had(self):
        """The reference bill: pasta shared by two, salmon alone, mocktails
        by all four, dessert for the host; $8.61 tax and $17.46 tip."""
        shares = item_shares(
            4,
            [(2850, [1, 3]), (3400, [2]), (1800, [0, 1, 2, 3]), (1650, [0])],
            861,
            1746,
        )

        assert shares == [2664, 2379, 4885, 2379]
        assert sum(shares) == 2850 + 3400 + 1800 + 1650 + 861 + 1746

    def test_without_tax_or_tip_each_pays_their_items(self):
        assert item_shares(3, [(18000, [0]), (16000, [1]), (12000, [2])], 0, 0) == [
            18000,
            16000,
            12000,
        ]

    def test_someone_with_no_items_pays_no_tax(self):
        shares = item_shares(3, [(1000, [0]), (1000, [1])], 300, 0)

        assert shares == [1150, 1150, 0]

    def test_an_item_nobody_had_is_refused(self):
        with pytest.raises(ValueError):
            item_shares(2, [(1000, [])], 0, 0)
