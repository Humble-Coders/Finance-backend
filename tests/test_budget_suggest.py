"""`suggest` — the budget's arithmetic, with no database (ticket #56).

Every rule the generator follows is a row here. The persistence around it is
covered in `test_budgets_api.py`; this file is where a wrong number would be
caught first.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.services.budget import (
    CategoryRef,
    median,
    savings_and_shortfall,
    suggest,
)


def ref(slug: str) -> CategoryRef:
    return CategoryRef(uuid.uuid5(uuid.NAMESPACE_DNS, slug), slug)


GROCERIES = ref("groceries")
DINING = ref("dining")
DEBT = ref("debt_payment")
OWN_DEBT = CategoryRef(uuid.uuid4(), "debt_payment")


def lines(history, income=0, minimums=0):
    return suggest(history, income, minimums, "CAD")


class TestTheMedian:
    @pytest.mark.parametrize(
        ("months", "expected"),
        [
            ([10_000, 30_000, 20_000], 20_000),  # three months: the middle one
            ([10_000, 20_100], 15_100),  # two: their mean, $150.50 -> $151
            ([12_345], 12_400),  # one: that month, rounded up
            ([0, 10_000, 20_000], 10_000),  # two of three: the zero counts
        ],
    )
    def test_a_category_is_budgeted_at_its_median(self, months, expected):
        assert lines({GROCERIES: months}).spending == {GROCERIES.id: expected}

    def test_a_one_off_gets_no_line(self):
        """Seen in one month of three, its median is the zero beside it."""
        suggestion = lines({GROCERIES: [10_000] * 3, DINING: [0, 0, 90_000]})
        assert suggestion.spending == {GROCERIES.id: 10_000}

    def test_an_even_count_keeps_the_half_until_rounding(self):
        assert median([1, 2]) == Decimal("1.5")
        assert median([]) == Decimal(0)


class TestRounding:
    def test_an_exact_unit_is_left_alone(self):
        assert lines({GROCERIES: [12_000]}).spending[GROCERIES.id] == 12_000

    def test_a_cent_above_a_unit_rounds_up_to_the_next(self):
        """$120.01 suggests $121: $120 is a budget already overspent."""
        assert lines({GROCERIES: [12_001]}).spending[GROCERIES.id] == 12_100


class TestTheDebtLine:
    def test_observed_payments_win_when_larger(self):
        assert lines({DEBT: [30_000] * 3}, minimums=25_000).debt == 30_000

    def test_minimums_win_when_larger(self):
        assert lines({DEBT: [30_000] * 3}, minimums=40_000).debt == 40_000

    def test_minimums_with_no_payments_seen(self):
        assert lines({}, minimums=12_550).debt == 12_550

    def test_no_debts_and_no_payments_is_no_line(self):
        assert lines({GROCERIES: [10_000]}).debt == 0

    def test_debt_payments_are_not_a_spending_line(self):
        assert DEBT.id not in lines({DEBT: [30_000]}).spending

    def test_a_household_s_own_debt_category_folds_into_the_one_line(self):
        suggestion = lines({DEBT: [10_000, 10_000], OWN_DEBT: [5_000, 5_000]})
        assert suggestion.debt == 15_000
        assert OWN_DEBT.id not in suggestion.spending


class TestSavingsAndShortfall:
    def test_savings_is_the_positive_remainder(self):
        suggestion = lines(
            {GROCERIES: [60_000], DEBT: [40_000]}, income=500_000, minimums=0
        )
        assert suggestion.savings == 400_000
        assert suggestion.shortfall is None

    def test_lines_above_income_are_a_shortfall_and_no_savings(self):
        suggestion = lines({GROCERIES: [150_000]}, income=100_000)
        assert suggestion.savings == 0
        assert suggestion.shortfall == 50_000

    def test_exactly_balanced_is_neither(self):
        assert savings_and_shortfall(100_000, 100_000) == (0, None)

    def test_zero_income_makes_every_line_a_shortfall(self):
        suggestion = lines({GROCERIES: [10_000]}, income=0, minimums=5_000)
        assert suggestion.savings == 0
        assert suggestion.shortfall == 15_000

    def test_zero_income_and_nothing_spent_is_an_empty_budget(self):
        assert lines({}) == suggest({}, 0, 0, "CAD")
        assert lines({}).spending == {}
        assert lines({}).shortfall is None

    def test_observed_savings_debits_are_not_a_line(self):
        """Savings is the remainder of income, not a median of transfers out."""
        suggestion = lines({ref("savings"): [90_000] * 3}, income=100_000)
        assert suggestion.spending == {}
        assert suggestion.savings == 100_000


class TestWhatIsNeverALine:
    @pytest.mark.parametrize("slug", ["income", "transfers"])
    def test_money_moving_is_not_money_spent(self, slug):
        assert lines({ref(slug): [100_000] * 3}).spending == {}
