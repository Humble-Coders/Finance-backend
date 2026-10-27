"""The spending check's own arithmetic and its reading of the model's answer:
no database."""

from __future__ import annotations

from app.services.spending_check import _advice, share_percent


def test_a_share_is_a_whole_percentage_rounded_half_up():
    assert share_percent(45_001, 500_000) == 9
    assert share_percent(2_500, 500_000) == 1, "half a percent rounds up"
    assert share_percent(2_499, 500_000) == 0
    assert share_percent(500_000, 500_000) == 100


def test_no_income_is_no_share():
    assert share_percent(10_000, 0) == 0
    assert share_percent(-5, 100) == 0


def test_only_known_categories_and_figure_free_words_are_kept():
    advice = _advice(
        {
            "headline": "Wants are high. They took 41% of income.",
            "tips": ["Cook at home more.", 7, "Save $200.", "Keep going."],
            "categories": [
                {"category": "dining", "tip": "Plan meals out."},
                {"category": "dining", "tip": "A second tip for the same one."},
                {"category": "crypto", "tip": "Not a category of theirs."},
                "not even an object",
            ],
        },
        {"dining", "groceries"},
    )

    assert advice["headline"] == "Wants are high."
    assert advice["tips"] == ["Cook at home more."], "three read; the rest dropped"
    assert advice["categories"] == [{"category": "dining", "tip": "Plan meals out."}]
