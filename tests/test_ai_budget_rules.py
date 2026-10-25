"""The AI budget's arithmetic, with no database and no model.

Whatever the model answers, `checked` must hand back lines that hold their
commitments, keep wants within their share, and add up — with the Debt line
and savings — to the income exactly. These are the guarantees that let a
model's suggestion be shown at all.
"""

from __future__ import annotations

import pytest

from app.services.ai_budget import (
    WANT_SLUGS,
    checked,
    envelope,
    figure_free,
)

INCOME = 500_000  # $5,000.00


def env(floors=None, debt=12_000, goals=0, income=INCOME):
    return envelope(income, floors or {"rent": 180_000}, debt, goals, "CAD")


class TestTheEnvelope:
    def test_savings_is_a_fifth_of_income_or_what_the_goals_need(self):
        assert env().savings == 100_000
        assert env(goals=150_000).savings == 150_000

    def test_what_is_flexible_is_what_the_fixed_parts_leave(self):
        e = env()
        assert e.flexible == INCOME - 180_000 - 12_000 - 100_000

    def test_fixed_costs_above_four_fifths_shrink_savings_and_say_so(self):
        e = env(floors={"rent": 420_000}, debt=50_000)
        assert e.savings == 30_000
        assert e.flexible == 0
        assert e.savings_short is True

    def test_a_commitment_rounds_up_and_money_moving_is_never_a_floor(self):
        e = env(floors={"subscriptions": 1_649, "transfers": 40_000, "savings": 5_000})
        assert e.floors == {"subscriptions": 1_700}


class TestWhatTheModelSaysIsMadeToFit:
    @pytest.mark.parametrize(
        "proposed",
        [
            {
                "groceries": 60_000,
                "dining": 90_000,
                "shopping": 200_000,
                "rent": 150_000,
            },
            {"groceries": 10_000_000},
            {},
            {"dining": 49_999, "travel": 1, "income": 900_000, "savings": 10_000},
        ],
    )
    def test_lines_debt_and_savings_add_up_to_income(self, proposed):
        e = env(floors={"rent": 180_000, "subscriptions": 1_649})
        result = checked(e, proposed, {}, "CAD")

        assert (
            sum(line.amount for line in result.lines) + result.debt + result.savings
            == INCOME
        )
        assert result.savings >= e.savings
        lines = {line.slug: line.amount for line in result.lines}
        assert lines["rent"] >= 180_000, "a commitment is the least its line holds"
        assert lines["subscriptions"] >= 1_700
        assert sum(a for s, a in lines.items() if s in WANT_SLUGS) <= e.wants_cap
        assert "income" not in lines and "savings" not in lines
        assert all(a % 100 == 0 for a in lines.values()), "whole dollars"

    def test_unused_flexible_money_goes_to_savings(self):
        e = env()
        result = checked(e, {"groceries": 40_000}, {}, "CAD")
        assert result.savings == INCOME - 180_000 - 40_000 - 12_000

    def test_reasons_are_kept_with_their_lines(self):
        result = checked(
            env(), {"groceries": 40_000}, {"groceries": "Food for one."}, "CAD"
        )
        assert {line.slug: line.reason for line in result.lines}[
            "groceries"
        ] == "Food for one."


class TestNoFigureInTheWords:
    def test_a_sentence_with_a_digit_is_dropped_whole(self):
        text = "Groceries rose. You spent $52 more on dining. Keep the buffer first."
        assert figure_free(text, 200) == "Groceries rose. Keep the buffer first."

    def test_nothing_left_is_none(self):
        assert figure_free("Spend 20% less.", 200) is None
        assert figure_free(None, 200) is None
        assert figure_free(42, 200) is None
