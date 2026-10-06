"""`project` — a goal's arithmetic, with no database (backend #65).

Every rule of projection v1 is a row here. The persistence around it is in
`test_goals_api.py`. Amounts are minor units: 100_000 is $1,000.00.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date
from types import SimpleNamespace

import pytest

from app.services import goals
from app.services.goals import (
    ACHIEVED,
    BEHIND,
    ON_TRACK,
    OPEN,
    OVERDUE,
    monthly_need,
    months_through,
    project,
)

TODAY = date(2026, 9, 15)


def run(target=100_000, saved=0, on=None, monthly=None, today=TODAY):
    return project(target, saved, on, monthly, today)


class TestMonthsToContribute:
    @pytest.mark.parametrize(
        ("target_date", "months"),
        [
            (date(2026, 9, 30), 1),  # later this month: this month is the one
            (date(2026, 10, 1), 2),  # next month: this and next
            (date(2026, 11, 30), 3),
            (date(2027, 2, 1), 6),  # across the year
        ],
    )
    def test_counted_from_this_month_through_the_target_s(self, target_date, months):
        assert months_through(TODAY, target_date) == months


class TestTheProjection:
    def test_a_date_only_gives_a_monthly_need_and_stays_open(self):
        result = run(on=date(2026, 11, 30))

        assert result.months == 3
        assert result.required_monthly == 33_334  # $1,000.00 / 3 → $333.34
        assert result.projected_completion is None
        assert result.status == OPEN

    def test_the_need_rounds_up_so_the_months_never_fall_short(self):
        result = run(on=date(2026, 11, 30))
        assert result.required_monthly * result.months >= result.remaining

    def test_a_currency_without_cents_rounds_up_to_the_whole_unit(self):
        """¥1,000 over 3 months: the minor unit is the yen, so 334, never 333."""
        assert run(target=1_000, on=date(2026, 11, 30)).required_monthly == 334

    def test_a_target_later_this_month_asks_for_all_of_it_now(self):
        assert run(on=date(2026, 9, 30)).required_monthly == 100_000

    def test_a_contribution_only_gives_a_completion_month_and_stays_open(self):
        result = run(monthly=25_000)

        assert result.projected_completion == date(2026, 12, 1)  # Sep, Oct, Nov, Dec
        assert result.required_monthly is None
        assert result.status == OPEN

    def test_a_contribution_past_what_remains_completes_this_month(self):
        assert run(saved=90_000, monthly=50_000).projected_completion == date(
            2026, 9, 1
        )

    def test_a_contribution_of_zero_completes_never(self):
        assert run(monthly=0).projected_completion is None

    @pytest.mark.parametrize(
        ("monthly", "status"),
        [(33_334, ON_TRACK), (40_000, ON_TRACK), (33_333, BEHIND)],
    )
    def test_both_say_whether_the_plan_keeps_pace(self, monthly, status):
        assert run(on=date(2026, 11, 30), monthly=monthly).status == status

    def test_neither_is_open_with_nothing_to_say(self):
        result = run()

        assert result.status == OPEN
        assert result.required_monthly is None
        assert result.projected_completion is None
        assert result.remaining == 100_000

    @pytest.mark.parametrize("saved", [100_000, 150_000])
    def test_reaching_the_target_is_achieved_and_never_past_100(self, saved):
        result = run(saved=saved, on=date(2025, 1, 1), monthly=10_000)

        assert result.status == ACHIEVED, "achieved outranks overdue"
        assert result.progress_percent == 100
        assert result.remaining == 0
        assert result.projected_completion is None

    def test_a_date_that_has_passed_is_overdue_with_no_monthly_need(self):
        result = run(saved=10_000, on=date(2026, 9, 14), monthly=10_000)

        assert result.status == OVERDUE
        assert result.required_monthly is None
        assert result.months is None

    def test_progress_rounds_down(self):
        assert run(target=1_000, saved=333).progress_percent == 33
        assert run(target=1_000, saved=999).progress_percent == 99


class TestTheMonthsNeed:
    def goal(self, monthly=None):
        return SimpleNamespace(monthly_contribution_minor_units=monthly)

    def test_a_dated_goal_needs_its_required_amount(self):
        assert monthly_need(self.goal(5_000), run(on=date(2026, 11, 30))) == 33_334

    def test_an_undated_or_overdue_goal_needs_what_was_planned(self):
        assert monthly_need(self.goal(5_000), run()) == 5_000
        assert monthly_need(self.goal(5_000), run(on=date(2026, 1, 1))) == 5_000
        assert monthly_need(self.goal(None), run()) == 0

    def test_an_achieved_goal_needs_nothing(self):
        assert monthly_need(self.goal(5_000), run(saved=100_000)) == 0


class TestDeterminism:
    def test_the_same_inputs_give_the_same_projection(self):
        first = run(saved=12_345, on=date(2027, 3, 31), monthly=7_000)
        assert first == run(saved=12_345, on=date(2027, 3, 31), monthly=7_000)

    def test_no_float_in_the_module(self):
        """Integer minor units throughout: no float literal and no `float`."""
        tree = ast.parse(inspect.getsource(goals))
        floats = [
            node
            for node in ast.walk(tree)
            if (isinstance(node, ast.Constant) and isinstance(node.value, float))
            or (isinstance(node, ast.Name) and node.id == "float")
            or (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div))
        ]
        assert floats == [], "true division makes a float; use integer division"

    def test_v1_says_what_it_is(self):
        assert goals.PROJECTION_VERSION == "v1"
        assert goals.ASSUMES_GROWTH is False
