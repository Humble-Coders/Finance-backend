"""Tests for the money boundary. Highest-value tests in the codebase."""

import pytest

from app.core.money import (
    MoneyError,
    exponent_for,
    from_minor_units,
    normalize,
    to_minor_units,
)


class TestToMinorUnits:
    @pytest.mark.parametrize(
        ("amount", "currency", "expected"),
        [
            ("1200.50", "CAD", 120050),
            ("1200", "CAD", 120000),
            ("0", "CAD", 0),
            ("0.01", "CAD", 1),
            ("-42.10", "CAD", -4210),
            ("1200", "JPY", 1200),
            (" 12.34 ", "CAD", 1234),
        ],
    )
    def test_converts(self, amount, currency, expected):
        assert to_minor_units(amount, currency) == expected

    def test_rejects_excess_precision(self):
        # Silently rounding here is how fractions of a cent go missing.
        with pytest.raises(MoneyError, match="more precision"):
            to_minor_units("12.345", "CAD")

    def test_rounds_when_explicitly_allowed(self):
        assert to_minor_units("12.345", "CAD", allow_rounding=True) == 1235

    def test_jpy_has_no_minor_units(self):
        with pytest.raises(MoneyError):
            to_minor_units("1200.50", "JPY")

    @pytest.mark.parametrize("bad", ["", "   ", "abc", "1,200.00", "NaN", "Infinity"])
    def test_rejects_garbage(self, bad):
        with pytest.raises(MoneyError):
            to_minor_units(bad, "CAD")


class TestFromMinorUnits:
    @pytest.mark.parametrize(
        ("minor", "currency", "expected"),
        [
            (120050, "CAD", "1200.50"),
            (0, "CAD", "0.00"),
            (1, "CAD", "0.01"),
            (-4210, "CAD", "-42.10"),
            (1200, "JPY", "1200"),
        ],
    )
    def test_formats(self, minor, currency, expected):
        assert from_minor_units(minor, currency) == expected

    def test_rejects_non_int(self):
        with pytest.raises(MoneyError):
            from_minor_units(12.5, "CAD")  # type: ignore[arg-type]


class TestRoundTrip:
    @pytest.mark.parametrize(
        "amount", ["0", "0.01", "1200.50", "-42.10", "999999999.99"]
    )
    def test_survives_round_trip(self, amount):
        currency = "CAD"
        assert normalize(amount, currency) == normalize(
            from_minor_units(to_minor_units(amount, currency), currency), currency
        )

    def test_normalize_makes_equal_amounts_comparable(self):
        assert normalize("1200", "CAD") == normalize("1200.00", "CAD")


def test_exponent_lookup():
    assert exponent_for("CAD") == 2
    assert exponent_for("jpy") == 0
    assert exponent_for("XYZ") == 2  # unknown currencies default to 2
    with pytest.raises(MoneyError):
        exponent_for("CA")
