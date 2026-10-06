"""`score` — the Money Health Score's arithmetic, with no database (ticket #57).

Every rule of formula v1 is a row here. Reading and keeping snapshots is
covered in `test_health_score_api.py`.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date
from decimal import Decimal

import pytest

from app.services import health_score
from app.services.health_score import (
    DEBT_PAYMENTS,
    SAVINGS_CONSISTENCY,
    SPENDING_VS_BUDGET,
    BudgetLineUse,
    DebtPicture,
    MonthFlow,
    ScoreInputs,
    debt_payments,
    inputs_from_json,
    result_from_json,
    savings_consistency,
    score,
    spending_vs_budget,
    to_json,
)

NO_DEBTS = DebtPicture(debts=0, debts_with_minimum=0, required=0, paid=0)
UNSCORABLE_DEBTS = DebtPicture(debts=1, debts_with_minimum=0, required=0, paid=0)


def month(n: int, income: int, expenses: int) -> MonthFlow:
    return MonthFlow(date(2026, n, 1), income, expenses)


def line(allocated: int, spent: int, slug: str = "groceries") -> BudgetLineUse:
    return BudgetLineUse(slug, allocated, spent)


def inputs(months=(), lines=(), debt=NO_DEBTS) -> ScoreInputs:
    return ScoreInputs(tuple(months), tuple(lines), debt)


def component(result, key):
    return next(c for c in result.components if c.key == key)


class TestSavingsConsistency:
    @pytest.mark.parametrize(
        ("income", "expenses", "expected"),
        [
            (100_000, 100_000, 0),  # saved nothing
            (100_000, 80_000, 100),  # saved 20 %: full marks
            (100_000, 90_000, 50),  # 10 % is half of 20 %
            (100_000, 70_000, 100),  # 30 % scores the same as 20 %: the cap
            (100_000, 150_000, 0),  # a loss month is 0, never negative
        ],
    )
    def test_a_month_s_rate_against_the_cap(self, income, expenses, expected):
        assert savings_consistency((month(8, income, expenses),)) == expected

    def test_the_months_are_averaged(self):
        months = (month(6, 100_000, 80_000), month(7, 100_000, 100_000))
        assert savings_consistency(months) == 50

    def test_a_month_with_no_income_is_left_out(self):
        months = (month(6, 100_000, 80_000), month(7, 0, 40_000))
        assert savings_consistency(months) == 100

    def test_no_income_in_any_month_is_unavailable(self):
        assert savings_consistency((month(8, 0, 40_000),)) is None
        assert savings_consistency(()) is None


class TestSpendingVsBudget:
    @pytest.mark.parametrize(
        ("allocated", "spent", "expected"),
        [
            (40_000, 30_000, 100),  # under
            (40_000, 40_000, 100),  # exactly on
            (40_000, 50_000, 75),  # 25 % over
            (40_000, 80_000, 0),  # 100 % over is 0
            (40_000, 200_000, 0),  # and beyond it stays 0
        ],
    )
    def test_a_line(self, allocated, spent, expected):
        assert spending_vs_budget((line(allocated, spent),)) == expected

    def test_lines_are_weighted_by_allocation(self):
        """$300 within budget and $100 at 100 % over: (100×3 + 0×1) / 4."""
        lines = (line(30_000, 10_000), line(10_000, 20_000, "dining"))
        assert spending_vs_budget(lines) == 75

    def test_no_lines_or_only_zero_lines_are_unavailable(self):
        assert spending_vs_budget(()) is None
        assert spending_vs_budget((line(0, 5_000),)) is None


class TestDebtPayments:
    def test_no_debts_scores_100(self):
        assert debt_payments(NO_DEBTS) == 100

    def test_debts_with_no_minimum_are_unavailable(self):
        assert debt_payments(UNSCORABLE_DEBTS) is None

    @pytest.mark.parametrize(
        ("paid", "expected"), [(0, 0), (12_500, 50), (25_000, 100), (90_000, 100)]
    )
    def test_paid_against_required(self, paid, expected):
        debt = DebtPicture(debts=2, debts_with_minimum=1, required=25_000, paid=paid)
        assert debt_payments(debt) == expected


class TestTheScore:
    def test_every_component_at_100(self):
        result = score(
            inputs([month(8, 100_000, 80_000)], [line(40_000, 40_000)], NO_DEBTS)
        )
        assert result.score == 100
        assert result.formula_version == "v1"

    def test_every_component_at_0(self):
        debt = DebtPicture(debts=1, debts_with_minimum=1, required=25_000, paid=0)
        result = score(inputs([month(8, 100_000, 100_000)], [line(1, 2)], debt))
        assert result.score == 0

    def test_the_weights_are_40_35_25(self):
        """Savings 100, spending 0, debt 100: (40 + 25) / 100."""
        result = score(
            inputs([month(8, 100_000, 80_000)], [line(40_000, 80_000)], NO_DEBTS)
        )
        assert result.score == 65

    def test_a_missing_component_is_renormalised_away(self):
        """Savings 100 and debt 0, no budget: (40×100 + 25×0) / 65 = 61.5 → 62,
        not 40 dragged down by the missing 35."""
        debt = DebtPicture(debts=1, debts_with_minimum=1, required=25_000, paid=0)
        result = score(inputs([month(8, 100_000, 80_000)], [], debt))
        assert result.score == 62
        spending = component(result, SPENDING_VS_BUDGET)
        assert spending.available is False
        assert spending.weight == 0
        assert component(result, SAVINGS_CONSISTENCY).weight == Decimal(4000) / 65
        assert component(result, DEBT_PAYMENTS).weight == Decimal(2500) / 65

    def test_nothing_available_is_no_score(self):
        result = score(inputs([], [], UNSCORABLE_DEBTS))
        assert result.score is None
        assert all(not c.available for c in result.components)

    def test_half_rounds_up(self):
        """12.5 % saved scores 62.5; alone, that is the score, and .5 goes up."""
        result = score(inputs([month(8, 100_000, 87_500)], [], UNSCORABLE_DEBTS))
        assert component(result, SAVINGS_CONSISTENCY).score == Decimal("62.5")
        assert result.score == 63

    def test_the_overall_score_uses_exact_parts_not_rounded_ones(self):
        """Savings 50.5 (shown as 51) and spending 50.4 (shown as 50). From the
        rounded parts the score would be 50.53 → 51; from the exact ones it is
        50.45 → 50, and only the exact one is reproducible."""
        months = [month(8, 1_000_000, 899_000)]  # 10.1 % saved → 50.5
        lines = [line(1_000_000, 1_496_000)]  # 49.6 % over → 50.4
        result = score(inputs(months, lines, UNSCORABLE_DEBTS))
        assert component(result, SAVINGS_CONSISTENCY).display_score == 51
        assert component(result, SPENDING_VS_BUDGET).display_score == 50
        assert result.score == 50


class TestDeterminism:
    def test_equal_inputs_give_equal_results(self):
        a = inputs(
            [month(6, 100_000, 90_000), month(7, 80_000, 60_000)],
            [line(40_000, 50_000), line(10_000, 2_000, "dining")],
            DebtPicture(debts=2, debts_with_minimum=2, required=30_000, paid=20_000),
        )
        b = inputs(
            [month(6, 100_000, 90_000), month(7, 80_000, 60_000)],
            [line(40_000, 50_000), line(10_000, 2_000, "dining")],
            DebtPicture(debts=2, debts_with_minimum=2, required=30_000, paid=20_000),
        )
        assert score(a) == score(b)

    def test_order_does_not_matter(self):
        debt = DebtPicture(debts=1, debts_with_minimum=1, required=30_000, paid=20_000)
        months = [month(6, 100_000, 90_000), month(7, 80_000, 60_000)]
        lines = [line(40_000, 50_000), line(10_000, 2_000, "dining")]
        forward = score(inputs(months, lines, debt))
        backward = score(inputs(months[::-1], lines[::-1], debt))
        assert forward == backward


class TestTheStoredBreakdown:
    def test_it_reproduces_the_score(self):
        original = inputs(
            [month(7, 80_000, 60_000), month(6, 100_000, 90_000)],
            [line(10_000, 2_000, "dining"), line(40_000, 50_000)],
            DebtPicture(debts=2, debts_with_minimum=1, required=30_000, paid=20_000),
        )
        result = score(original)

        replayed = score(inputs_from_json(to_json(original, result)))

        assert replayed == result

    def test_a_held_snapshot_is_read_back_as_stored_not_recomputed(self):
        """A snapshot from an older formula keeps its own score and parts."""
        original = inputs([month(8, 100_000, 90_000)], [line(40_000, 50_000)])
        stored = to_json(original, score(original))
        stored["components"][0]["score"] = "12.5"

        held = result_from_json(stored, 41, "v0")

        assert held.score == 41
        assert held.formula_version == "v0"
        assert held.components[0].score == Decimal("12.5")

    def test_it_records_the_formula_version(self):
        result = score(inputs([month(8, 100_000, 80_000)]))
        assert (
            to_json(inputs([month(8, 100_000, 80_000)]), result)["formula_version"]
            == "v1"
        )


class TestNoModelInvolved:
    def test_the_module_imports_no_llm_client(self):
        """The LLM explains a score; it never produces one (CLAUDE.md)."""
        tree = ast.parse(inspect.getsource(health_score))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not any(
            "llm" in name or "openai" in name or "httpx" in name for name in imported
        ), imported

    def test_no_float_in_the_module(self):
        """No float literal and no use of `float` — Decimal throughout."""
        tree = ast.parse(inspect.getsource(health_score))
        floats = [
            node
            for node in ast.walk(tree)
            if (isinstance(node, ast.Constant) and isinstance(node.value, float))
            or (isinstance(node, ast.Name) and node.id == "float")
        ]
        assert floats == []
