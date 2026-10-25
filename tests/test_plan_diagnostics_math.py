"""The diagnostics' one formula, with no database."""

from app.services.ai_budget import figure_free
from app.services.plan_diagnostics import ASSUMED_RETURN_BPS, _advice, first_year_gain


def test_a_year_of_monthly_payments_at_a_rate():
    # $100 a month for a year at 6%: 100 x 0.06 / 12 x 78 = $39.
    assert first_year_gain(10_000, 600) == 3_900
    assert first_year_gain(10_000, ASSUMED_RETURN_BPS) == 3_250


def test_only_known_steps_with_words_and_no_figures_are_kept():
    advice = _advice(
        "allocation",
        {
            "headline": "Savings first.",
            "steps": [
                {"to": "savings", "why": "A cushion first."},
                {"to": "debt", "why": "It costs 21% a year."},
                {"to": "crypto", "why": "No."},
                "not a step",
            ],
            "note": None,
        },
    )
    assert advice == {
        "headline": "Savings first.",
        "steps": [{"to": "savings", "why": "A cushion first."}],
        "note": None,
    }
    assert figure_free("Clear the card.", 100) == "Clear the card."
