"""Goal completion in the Money Health Score — formula v2 (backend #66).

The arithmetic only, with no database. "Last complete month" is September
2026 throughout, so "today" is in October.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pytest

from app.services.health_score import (
    BudgetLineUse,
    DebtPicture,
    GoalPace,
    MonthFlow,
    ScoreInputs,
    goal_completion,
    inputs_from_json,
    score,
    to_json,
)

LAST = date(2026, 9, 1)
NO_DEBTS = DebtPicture(debts=0, debts_with_minimum=0, required=0, paid=0)
UNSCORABLE_DEBTS = DebtPicture(debts=1, debts_with_minimum=0, required=0, paid=0)


def pace(
    saved,
    target=120_000,
    created=date(2026, 7, 1),
    due=date(2026, 12, 1),
    achieved=None,
):
    return GoalPace(target, saved, created, due, achieved)


def completion(*goals):
    return goal_completion(tuple(goals), LAST)


class TestPace:
    """Created July, due December: six months, three of them gone by the end
    of September, so it should hold half its target."""

    @pytest.mark.parametrize(
        ("saved", "expected"),
        [(60_000, 100), (30_000, 50), (90_000, 100), (0, 0)],
    )
    def test_saved_against_where_it_should_be(self, saved, expected):
        assert completion(pace(saved)) == expected

    def test_due_last_month_is_judged_against_the_whole_target(self):
        assert completion(pace(60_000, due=date(2026, 9, 1))) == 50

    def test_past_its_date_and_unfinished_is_judged_against_the_whole_target(self):
        assert (
            completion(pace(30_000, created=date(2026, 1, 1), due=date(2026, 6, 1)))
            == 25
        )


class TestWhichGoalsCount:
    @pytest.mark.parametrize("created", [date(2026, 9, 1), date(2026, 10, 1)])
    def test_one_created_last_month_or_this_month_has_no_pace_yet(self, created):
        assert completion(pace(60_000, created=created)) is None

    def test_one_without_a_date_has_no_pace_to_judge(self):
        assert completion(pace(60_000, due=None)) is None

    @pytest.mark.parametrize(
        ("achieved", "counted"),
        [
            (date(2026, 10, 1), True),  # this month
            (date(2025, 10, 1), True),  # eleven months before September
            (date(2025, 9, 1), False),  # twelve: it has had its year
        ],
    )
    def test_an_achieved_goal_counts_in_full_for_twelve_months(self, achieved, counted):
        goal = pace(120_000, created=date(2025, 1, 1), achieved=achieved)
        assert completion(goal) == (100 if counted else None)

    def test_an_achieved_goal_made_last_month_does_not_count(self):
        """Made and filled at once is not a year of full marks."""
        assert (
            completion(
                pace(120_000, created=date(2026, 9, 1), achieved=date(2026, 9, 1))
            )
            is None
        )

    def test_several_are_a_plain_average(self):
        big = pace(30_000, target=12_000_000)  # far behind: 0.5 % of where it should be
        small = pace(60_000)  # on pace
        assert completion(big, small) == (Decimal(100) * 30_000 / 6_000_000 + 100) / 2

    def test_none_counting_is_unavailable(self):
        assert completion() is None
        assert goal_completion((pace(60_000),), None) is None


class TestTheScore:
    def inputs(self, months=(), lines=(), debt=NO_DEBTS, goals=()):
        return ScoreInputs(tuple(months), tuple(lines), debt, tuple(goals), LAST)

    def test_goals_weigh_fifteen(self):
        """Savings, spending and debt at 100, goals at 50: 92.5 → 93."""
        result = score(
            self.inputs(
                [MonthFlow(date(2026, 9, 1), 100_000, 80_000)],
                [BudgetLineUse("groceries", 40_000, 40_000)],
                NO_DEBTS,
                [pace(30_000)],
            )
        )
        assert result.score == 93
        assert result.formula_version == "v3"
        goals = next(c for c in result.components if c.key == "goal_completion")
        assert goals.weight == 15
        assert goals.display_score == 50

    @staticmethod
    def v1(parts: dict[str, Decimal | None]) -> int:
        """What formula v1 gave: 40 / 35 / 25, renormalised over what scored."""
        weights = {"savings": 40, "spending": 35, "debt": 25}
        present = [k for k, v in parts.items() if v is not None]
        total = sum(parts[k] * weights[k] for k in present) / sum(
            weights[k] for k in present
        )
        return int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))

    @pytest.mark.parametrize(
        ("income", "expenses", "allocated", "spent", "debt"),
        [
            (100_000, 87_500, 40_000, 50_000, NO_DEBTS),
            (100_000, 90_000, 1_000_000, 1_496_000, UNSCORABLE_DEBTS),
            (1_000_000, 899_000, 1_000_000, 1_496_000, UNSCORABLE_DEBTS),
            (100_000, 150_000, 30_000, 10_000, DebtPicture(2, 1, 30_000, 20_000)),
            (0, 10_000, 30_000, 31_000, NO_DEBTS),
        ],
    )
    def test_with_no_goal_counting_it_is_exactly_v1(
        self, income, expenses, allocated, spent, debt
    ):
        from app.services.health_score import (
            debt_payments,
            savings_consistency,
            spending_vs_budget,
        )

        months = (MonthFlow(date(2026, 9, 1), income, expenses),)
        lines = (BudgetLineUse("groceries", allocated, spent),)
        expected = self.v1(
            {
                "savings": savings_consistency(months),
                "spending": spending_vs_budget(lines),
                "debt": debt_payments(debt),
            }
        )

        assert score(self.inputs(months, lines, debt)).score == expected

    def test_reordered_goals_give_the_same_score(self):
        goals = [
            pace(30_000),
            pace(10_000, target=50_000),
            pace(70_000, due=date(2027, 3, 1)),
        ]
        forward = score(self.inputs(goals=goals))
        backward = score(self.inputs(goals=goals[::-1]))
        assert forward == backward


class TestTheStoredBreakdown:
    def test_a_v2_breakdown_reproduces_its_score(self):
        original = ScoreInputs(
            (MonthFlow(date(2026, 9, 1), 100_000, 90_000),),
            (BudgetLineUse("dining", 10_000, 12_000),),
            NO_DEBTS,
            (
                pace(30_000),
                pace(120_000, created=date(2025, 1, 1), achieved=date(2026, 2, 1)),
            ),
            LAST,
        )
        result = score(original)

        assert score(inputs_from_json(to_json(original, result))) == result

    def test_a_v1_breakdown_reads_as_no_goals_and_reproduces_its_score(self):
        """Stored before v2, with no goals and no last month in it."""
        v1_stored = {
            "formula_version": "v1",
            "inputs": {
                "months": [
                    {"month": "2026-08-01", "income": 100_000, "expenses": 90_000}
                ],
                "budget_lines": [
                    {"slug": "groceries", "allocated": 40_000, "spent": 50_000}
                ],
                "debt": {"debts": 0, "debts_with_minimum": 0, "required": 0, "paid": 0},
            },
            "components": [],
        }
        v1_score = self.v1_of(v1_stored)

        inputs = inputs_from_json(v1_stored)

        assert inputs.goals == ()
        assert inputs.last_complete_month is None
        assert score(inputs).score == v1_score

    @staticmethod
    def v1_of(stored) -> int:
        # savings 50 (10 %), spending 75 (25 % over), debt 100 → (2000+2625+2500)/100.
        return 71
