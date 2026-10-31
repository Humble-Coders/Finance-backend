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
    DEBT,
    EMERGENCY,
    GOALS,
    SAVE_OR_INVEST,
    SNOWBALL,
    DebtTerms,
    GoalAsk,
    Limits,
    Preferences,
    Weight,
    checked,
    debt_first,
    edited,
    schedule,
    split,
    suggest_buffer,
    suggest_weights,
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


def prefs(*weights, buffer=0, line=LINE, strategy=AVALANCHE) -> Preferences:
    return Preferences(
        tuple(Weight(c, bps) for c, bps in weights), line, strategy, buffer
    )


HOUSE = GoalAsk(uuid.uuid4(), need=10_000, remaining=500_000)
TRIP = GoalAsk(uuid.uuid4(), need=0, remaining=100_000)
FUND = uuid.uuid4()

# Emergency fund $300 short; the house and the trip; $4,000 owed.
LIMITS = Limits(emergency=(FUND, 30_000), goals=(HOUSE, TRIP), debt=400_000)
CHOICES = prefs((EMERGENCY, 2000), (DEBT, 5000), (GOALS, 2000), (SAVE_OR_INVEST, 1000))


class TestTheSplit:
    def test_each_takes_its_percentage_in_order(self):
        """$1,200: 20% ($240) to the fund, 50% ($600) to debt, 20% ($240) to
        goals — the house's $100 to stay on pace, then the rest to it as well
        — and 10% ($120) to save or invest."""
        result = split(120_000, CHOICES, LIMITS)

        assert result.emergency == 24_000
        assert result.extra_debt == 60_000
        assert result.goals == ((HOUSE.id, 24_000),)
        assert result.save_or_invest == 12_000
        assert result.total == 120_000

    def test_what_a_category_cannot_use_goes_on_down_the_priorities(self):
        """$3,000: the fund's 20% is $600 but it only needs $300; the $300 over
        tops the next by priority — debt, up to what is owed — first."""
        result = split(300_000, CHOICES, LIMITS)

        assert result.emergency == 30_000
        assert result.extra_debt == 150_000 + 30_000
        assert result.total == 300_000

    def test_with_nothing_to_give_to_the_rest_is_saved_or_invested(self):
        empty = Limits(emergency=None, goals=(), debt=0)

        result = split(50_000, CHOICES, empty)

        assert (result.emergency, result.extra_debt, result.goals) == (0, 0, ())
        assert result.save_or_invest == 50_000

    def test_a_buffer_comes_off_the_top(self):
        result = split(120_000, prefs((DEBT, 10_000), buffer=20_000), LIMITS)

        assert result.buffer == 20_000
        assert result.extra_debt == 100_000
        assert result.total == 120_000

    def test_nothing_left_over_splits_nothing(self):
        assert split(0, CHOICES, LIMITS).total == 0


class TestTheSuggestedChoices:
    def test_a_category_with_nothing_to_give_to_is_left_out(self):
        weights = suggest_weights(Limits(emergency=None, goals=(HOUSE,), debt=0))

        assert [w.category for w in weights] == [GOALS, SAVE_OR_INVEST]
        assert sum(w.bps for w in weights) == 10_000

    def test_all_four_when_there_is_something_for_each(self):
        weights = suggest_weights(LIMITS)

        assert [w.category for w in weights] == [EMERGENCY, DEBT, GOALS, SAVE_OR_INVEST]
        assert sum(w.bps for w in weights) == 10_000


class TestAnAiProposalIsChecked:
    def test_too_much_is_taken_back_from_the_lowest_priority(self):
        proposed = {EMERGENCY: 30_000, DEBT: 80_000, GOALS: 0, SAVE_OR_INVEST: 40_000}

        result = checked(120_000, CHOICES, LIMITS, proposed, {})

        assert result.save_or_invest == 10_000
        assert result.total == 120_000

    def test_over_a_limit_is_cut_and_the_rest_goes_down_the_priorities(self):
        proposed = {EMERGENCY: 90_000, DEBT: 30_000, GOALS: 0, SAVE_OR_INVEST: 0}

        result = checked(120_000, CHOICES, LIMITS, proposed, {})

        assert result.emergency == 30_000, "it only needs $300"
        assert result.extra_debt == 90_000
        assert result.total == 120_000

    def test_a_goals_amount_is_kept_within_what_it_needs(self):
        proposed = {EMERGENCY: 0, DEBT: 0, GOALS: 0, SAVE_OR_INVEST: 0}

        result = checked(120_000, CHOICES, LIMITS, proposed, {TRIP.id: 500_000})

        assert dict(result.goals)[TRIP.id] == 100_000
        assert result.total == 120_000

    def test_a_negative_amount_is_none(self):
        proposed = {EMERGENCY: -5_000, DEBT: 120_000, GOALS: 0, SAVE_OR_INVEST: 0}

        result = checked(120_000, CHOICES, LIMITS, proposed, {})

        assert result.emergency == 0
        assert result.total == 120_000


class TestARowSetByHand:
    def test_more_for_one_row_comes_from_the_lowest_priority_first(self):
        current = split(120_000, CHOICES, LIMITS)

        result = edited(current, CHOICES, LIMITS, DEBT, 70_000)

        assert result.extra_debt == 70_000
        assert result.save_or_invest == 2_000, "save or invest gave its $100 first"
        assert result.total == 120_000

    def test_less_for_one_row_goes_to_the_highest_that_can_use_it(self):
        current = split(120_000, CHOICES, LIMITS)

        result = edited(current, CHOICES, LIMITS, DEBT, 50_000)

        assert result.emergency == 30_000, "the fund tops up to what it needs"
        assert result.total == 120_000

    def test_a_goal_row_is_set_by_its_id(self):
        current = split(120_000, CHOICES, LIMITS)

        result = edited(current, CHOICES, LIMITS, GOALS, 5_000, TRIP.id)

        assert dict(result.goals)[TRIP.id] == 5_000
        assert result.total == 120_000

    @pytest.mark.parametrize("amount", [-1, 130_000])
    def test_outside_what_the_row_can_hold_is_refused(self, amount):
        current = split(120_000, CHOICES, LIMITS)

        with pytest.raises(ValueError):
            edited(current, CHOICES, LIMITS, EMERGENCY, amount)

    def test_the_emergency_fund_is_not_set_past_what_it_needs(self):
        current = split(120_000, CHOICES, LIMITS)

        with pytest.raises(ValueError):
            edited(current, CHOICES, LIMITS, EMERGENCY, 40_000)


class TestDebtFirst:
    def test_the_goals_share_goes_to_debt_and_debt_leads(self):
        result = debt_first(CHOICES.weights)

        assert result[0] == Weight(DEBT, 7000)
        assert Weight(GOALS, 0) in result
        assert sum(w.bps for w in result) == 10_000


class TestTheSuggestedBuffer:
    def test_one_month_of_average_spending_rounded_up(self):
        assert suggest_buffer([310_045, 295_000, 320_010], None, "CAD") == 308_400

    def test_else_what_setup_was_told_else_nothing(self):
        assert suggest_buffer([], 300_000, "CAD") == 300_000
        assert suggest_buffer([], None, "CAD") is None
