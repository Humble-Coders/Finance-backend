"""The money boundary (PRD §4.4).

    Postgres stores  ->  integer minor units
    API transports   ->  decimal strings
    Clients use      ->  decimal strings

This module is the ONLY place that conversion happens. Nothing else in the
codebase may multiply or divide an amount to change its representation.

Rules:
  * Never float. `Decimal` throughout — binary floats cannot represent 0.10.
  * Negative amounts are legitimate (refunds, debts, overruns) and supported.
  * Excess precision RAISES by default rather than silently rounding. Losing a
    fraction of a cent quietly is how money bugs start; callers that genuinely
    want rounding (e.g. LLM-extracted values) must opt in explicitly.
"""

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import Numeric, cast, literal

__all__ = [
    "MoneyError",
    "exponent_for",
    "to_minor_units",
    "from_minor_units",
    "normalize",
    "round_up_to_whole_unit",
    "display",
    "sql_major_units",
]

# ISO 4217 minor-unit exponents. Extend as markets are added (PRD §4.6).
_EXPONENTS: dict[str, int] = {
    "CAD": 2,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "JPY": 0,
}

_DEFAULT_EXPONENT = 2

# Symbols for sentences a person reads. A currency without one is shown by its
# code after the amount ("1,200 JPY"), which is never wrong, only plainer.
_SYMBOLS: dict[str, str] = {"CAD": "$", "USD": "$", "EUR": "€", "GBP": "£"}


class MoneyError(ValueError):
    """Raised when an amount or currency cannot be handled safely."""


def exponent_for(currency: str) -> int:
    """Minor-unit exponent for an ISO 4217 code (CAD -> 2, JPY -> 0)."""
    if not currency or len(currency) != 3:
        raise MoneyError(f"invalid currency code: {currency!r}")
    return _EXPONENTS.get(currency.upper(), _DEFAULT_EXPONENT)


def _parse(amount: str) -> Decimal:
    if amount is None:
        raise MoneyError("amount is required")
    text = str(amount).strip()
    if not text:
        raise MoneyError("amount is required")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise MoneyError(f"not a decimal amount: {amount!r}") from exc
    if not value.is_finite():
        raise MoneyError(f"amount is not finite: {amount!r}")
    return value


def to_minor_units(amount: str, currency: str, *, allow_rounding: bool = False) -> int:
    """Decimal string -> integer minor units, for storage.

    >>> to_minor_units("1200.50", "CAD")
    120050
    >>> to_minor_units("-42", "CAD")
    -4200

    Raises MoneyError on more precision than the currency allows, unless
    `allow_rounding=True` (half-up).
    """
    value = _parse(amount)
    exponent = exponent_for(currency)
    quantum = Decimal(1).scaleb(-exponent)

    rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    if not allow_rounding and rounded != value:
        raise MoneyError(
            f"{amount!r} has more precision than {currency.upper()} allows "
            f"({exponent} decimal places); pass allow_rounding=True to accept rounding"
        )
    return int(rounded.scaleb(exponent))


def from_minor_units(minor_units: int, currency: str) -> str:
    """Integer minor units -> decimal string, for the API response.

    >>> from_minor_units(120050, "CAD")
    '1200.50'
    >>> from_minor_units(1200, "JPY")
    '1200'
    """
    if not isinstance(minor_units, int) or isinstance(minor_units, bool):
        raise MoneyError(
            f"minor units must be an int, got {type(minor_units).__name__}"
        )
    exponent = exponent_for(currency)
    value = Decimal(minor_units).scaleb(-exponent)
    return f"{value:.{exponent}f}"


def normalize(amount: str, currency: str, *, allow_rounding: bool = False) -> str:
    """Canonical decimal string for comparison ('1200' and '1200.00' agree)."""
    return from_minor_units(
        to_minor_units(amount, currency, allow_rounding=allow_rounding), currency
    )


def round_up_to_whole_unit(minor_units: int | Decimal, currency: str) -> int:
    """Minor units -> the next whole currency unit at or above it, in minor units.

    >>> round_up_to_whole_unit(12000, "CAD")
    12000
    >>> round_up_to_whole_unit(12001, "CAD")
    12100
    >>> round_up_to_whole_unit(Decimal("12000.5"), "CAD")
    12100

    For a suggested budget line: $412.37 of spending suggests $413, never $412,
    which would be a budget the household is already over. Accepts a Decimal
    because a median of two months can fall on half a minor unit. Ceiling, not
    half-up, so a negative input moves toward zero.
    """
    if isinstance(minor_units, bool) or not isinstance(minor_units, int | Decimal):
        raise MoneyError(
            f"minor units must be an int or Decimal, got {type(minor_units).__name__}"
        )
    unit = Decimal(1).scaleb(exponent_for(currency))
    whole = (Decimal(minor_units) / unit).to_integral_value(rounding=ROUND_CEILING)
    return int(whole * unit)


def display(amount: str, currency: str) -> str:
    """A decimal string as a person reads it: "$1,234.50", "-$12.00".

    Formatting only — digits grouped and a symbol added; the value is the
    string's own, to the minor unit, never rounded. For sentences the server
    writes (the AI coach's suggested answers, #84); the API itself keeps
    returning plain decimal strings.

    >>> display("1234.5", "CAD")
    '$1,234.50'
    >>> display("-12", "CAD")
    '-$12.00'
    >>> display("1200", "JPY")
    '1,200 JPY'
    """
    exponent = exponent_for(currency)
    value = _parse(amount).quantize(Decimal(1).scaleb(-exponent))
    if value != _parse(amount):
        raise MoneyError(
            f"{amount!r} has more precision than {currency.upper()} allows"
        )
    sign = "-" if value < 0 else ""
    digits = f"{abs(value):,.{exponent}f}"
    symbol = _SYMBOLS.get(currency.upper())
    return f"{sign}{symbol}{digits}" if symbol else f"{sign}{digits} {currency.upper()}"


def sql_major_units(column, currency: str):
    """A SQL expression: [column]'s minor units as an exact decimal amount
    ("12.34" for 1234 CAD), for a query that hands amounts to someone who
    must not have to know about minor units.

    For the AI coach's SQL sandbox (#70): its tables hold amounts, so a model's
    `sum(amount)` is dollars whatever it names the column. Numeric throughout
    — no float — and scaled to the currency's exponent, exactly.
    """
    exponent = exponent_for(currency)
    return cast(
        cast(column, Numeric) / literal(10**exponent, Numeric),
        Numeric(20, exponent),
    )
