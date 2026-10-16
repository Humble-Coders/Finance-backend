"""The plan's arithmetic (#76): pure functions, hand-checked figures.

Every expected figure below was worked out independently of `plan.py` — the
single-debt case month by month, the three-debt case with a plain loop over
the same rules — before being written here.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.services.plan import (
    AVALANCHE,
    SNOWBALL,
    DebtTerms,
    GoalAsk,
    Preferences,
    schedule,
    split,
    suggest_buffer,
)

NOV = date(2026, 11, 1)
LINE = 600  # 6.00%


def debt(name: str, balance: int, minimum: int, rate: int | None, **promo) -> DebtTerms:
    return DebtTerms(
        uuid.uuid5(uuid.NAMESPACE_DNS, name), name, balance, minimum, rate, **promo
    )


def by_name(result, debts):
    outcomes = {o.id: o for o in result.debts}
    return {d.name: outcomes[d.id] for d in debts}


VISA = debt("Visa", 400_000, 12_000, 2099)
LOC = debt("Line of credit", 500_000, 15_000, 950)
CAR = debt("Car loan", 1_200_000, 35_000, 690)


class TestOneDebt:
    def test_a_month_by_month_payoff(self):
        """$1,000 at 12% (1% a month), $100 minimum. Month 1: $10.00 interest,
        $1,010.00, paid to $910.00. Month 2: $9.10, $919.10, to $819.10 …
        eleven payments, $58.98 of interest in all."""
        card = debt("Card", 100_000, 10_000, 1200)

        result = schedule([card], 0, NOV, LINE, AVALANCHE)

        outcome = result.debts[0]
        assert outcome.paid_off == date(2027, 9, 1), "the 11th month, from November"
        assert outcome.interest == 5_898
        assert result.total_interest == 5_898
        assert result.debt_free == date(2027, 9, 1)

    def test_interest_is_rounded_half_up_each_month(self):
        """$100.50 at 12%: $1.005 of interest rounds to $1.01, not $1.00."""
        card = debt("Card", 10_050, 20_000, 1200)

        assert schedule([card], 0, NOV, LINE, AVALANCHE).total_interest == 101


class TestAvalanche:
    def test_the_highest_rate_is_paid_first_and_its_payment_rolls_on(self):
        debts = [CAR, LOC, VISA]  # listed out of order on purpose

        result = schedule(debts, 90_000, NOV, LINE, AVALANCHE)

        named = by_name(result, debts)
        assert [named[d.name].order for d in (VISA, LOC, CAR)] == [1, 2, 3]
        assert [named[d.name].paid_off for d in (VISA, LOC, CAR)] == [
            date(2027, 3, 1),
            date(2027, 7, 1),
            date(2028, 1, 1),
        ]
        assert [named[d.name].interest for d in (VISA, LOC, CAR)] == [
            18_073,
            24_603,
            72_074,
        ]
        assert result.total_interest == 114_750
        assert result.debt_free == date(2028, 1, 1)

    def test_paying_only_minimums_costs_more_and_takes_longer(self):
        debts = [VISA, LOC, CAR]
        plan = schedule(debts, 90_000, NOV, LINE, AVALANCHE)
        minimums = schedule(debts, 0, NOV, LINE, AVALANCHE, rollover=False)

        assert minimums.total_interest > plan.total_interest
        assert minimums.debt_free > plan.debt_free


class TestSnowball:
    def test_the_smallest_balance_goes_first(self):
        """A small, cheap debt beside a large, dear one: snowball clears the
        small one first, and costs more than avalanche."""
        small = debt("Store card", 50_000, 2_500, 900)
        large = debt("Visa big", 600_000, 18_000, 2299)

        snow = schedule([small, large], 50_000, NOV, LINE, SNOWBALL)
        ava = schedule([small, large], 50_000, NOV, LINE, AVALANCHE)

        assert by_name(snow, [small, large])["Store card"].order == 1
        assert by_name(ava, [small, large])["Visa big"].order == 1
        assert snow.total_interest > ava.total_interest


class TestTheRateLine:
    def test_a_debt_below_the_line_only_ever_gets_its_minimum(self):
        mortgage = debt("Mortgage", 30_000_000, 150_000, 450)
        debts = [VISA, mortgage]

        result = schedule(debts, 90_000, NOV, LINE, AVALANCHE)

        named = by_name(result, debts)
        assert named["Visa"].order == 1
        assert named["Mortgage"].minimum_only is True
        assert named["Mortgage"].order is None
        assert named["Visa"].paid_off == date(2027, 3, 1), "all the extra on the Visa"

    def test_a_promotion_ending_moves_the_debt_into_the_order(self):
        """0% until May 2027 (below the line), 22.99% after: it gets only its
        minimum until then, then joins and — dearest — is paid first."""
        transfer = debt(
            "Balance transfer",
            250_000,
            5_000,
            2299,
            promo_rate_bps=0,
            promo_ends_on=date(2027, 5, 1),
        )
        debts = [transfer, CAR]

        result = schedule(debts, 50_000, NOV, LINE, AVALANCHE)

        named = by_name(result, debts)
        assert named["Balance transfer"].minimum_only is False
        assert named["Balance transfer"].order == 1
        assert named["Balance transfer"].interest > 0, "charged only after May"


class TestEdges:
    def test_a_payment_below_the_interest_is_never_paid_off_and_does_not_loop(self):
        """$10,000 at 24% is $200 of interest a month; a $50 minimum never
        catches up."""
        hopeless = debt("Hopeless", 1_000_000, 5_000, 2400)

        result = schedule([hopeless], 0, NOV, LINE, AVALANCHE)

        assert result.debts[0].never_paid_off is True
        assert result.debts[0].paid_off is None
        assert result.total_interest is None
        assert result.debt_free is None

    def test_a_debt_without_a_rate_is_left_out_not_guessed(self):
        unknown = debt("Unknown", 300_000, 10_000, None)
        debts = [VISA, unknown]

        result = schedule(debts, 90_000, NOV, LINE, AVALANCHE)

        named = by_name(result, debts)
        assert named["Unknown"].missing_rate is True
        assert named["Unknown"].interest == 0
        assert named["Visa"].order == 1
        assert result.debt_free is None, "not debt-free while one debt is unknown"

    def test_a_debt_already_at_zero_is_paid(self):
        nothing = debt("Paid card", 0, 0, 1999)

        result = schedule([nothing], 0, NOV, LINE, AVALANCHE)

        assert result.debts[0].paid_off == NOV
        assert result.total_interest == 0


def prefs(
    buffer=None, saved=0, share=2000, line=LINE, strategy=AVALANCHE
) -> Preferences:
    return Preferences(buffer, saved, share, line, strategy)


HOUSE = GoalAsk(uuid.uuid4(), need=10_000, remaining=500_000)
TRIP = GoalAsk(uuid.uuid4(), need=0, remaining=100_000)


class TestTheSplit:
    def test_buffer_then_goal_share_then_debt(self):
        """$1,200 left: $800 tops the buffer up to $1,000; 20% of the $400 left
        ($80) to goals — the house asks $100, gets $80; $320 to the Visa."""
        result = split(
            120_000, prefs(buffer=100_000, saved=20_000), [HOUSE, TRIP], [VISA], NOV
        )

        assert result.buffer == 80_000
        assert result.goals == ((HOUSE.id, 8_000),)
        assert (result.extra_debt, result.save_or_invest) == (32_000, 0)
        assert result.total == 120_000

    def test_with_no_debt_above_the_line_goals_then_save_or_invest(self):
        mortgage = debt("Mortgage", 30_000_000, 150_000, 450)

        result = split(120_000, prefs(), [HOUSE, TRIP], [mortgage], NOV)

        assert result.goals == ((HOUSE.id, 10_000), (TRIP.id, 100_000))
        assert (result.extra_debt, result.save_or_invest) == (0, 10_000)
        assert result.total == 120_000

    @pytest.mark.parametrize(
        ("share", "to_goals", "to_debt"), [(0, 0, 120_000), (10_000, 110_000, 10_000)]
    )
    def test_a_goal_share_of_none_or_all(self, share, to_goals, to_debt):
        """0%: everything to the debt. 100%: goals take what they need (the
        house $100 on pace, the trip all $1,000 it still needs) and only the
        rest goes to the debt."""
        result = split(120_000, prefs(share=share), [HOUSE, TRIP], [VISA], NOV)

        assert sum(a for _, a in result.goals) == to_goals
        assert result.extra_debt == to_debt
        assert result.total == 120_000

    def test_a_full_buffer_takes_nothing_and_no_buffer_takes_nothing(self):
        full = split(
            50_000, prefs(buffer=100_000, saved=100_000, share=0), [], [VISA], NOV
        )
        none = split(50_000, prefs(buffer=None, share=0), [], [VISA], NOV)

        assert full.buffer == none.buffer == 0
        assert full.extra_debt == none.extra_debt == 50_000

    def test_nothing_left_over_splits_nothing(self):
        result = split(0, prefs(buffer=100_000), [HOUSE], [VISA], NOV)

        assert result.total == 0


class TestTheSuggestedBuffer:
    def test_one_month_of_average_spending_rounded_up(self):
        assert suggest_buffer([310_045, 295_000, 320_010], None, "CAD") == 308_400

    def test_else_what_setup_was_told_else_nothing(self):
        assert suggest_buffer([], 300_000, "CAD") == 300_000
        assert suggest_buffer([], None, "CAD") is None
