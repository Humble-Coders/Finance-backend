"""Every figure in an AI coach reply must come from a tool result (#71).

`CLAUDE.md` → Financial math: the LLM never produces a number. The prompt
says so, but a prompt is a request; this checks. After the model answers, the
server reads every figure out of the reply — amounts, percentages, counts —
and looks for each among the values the turn's tools returned, as the same
decimal value. A figure found nowhere is ungrounded, and the reply is retried
or replaced (`app/services/chat.py`).

What is not checked, deliberately:

* **Dates and years** ("2016", "March 3", "2021-04") — they name periods the
  person asked about, not amounts.
* **Small counts, 0–10** ("your top 3 merchants", "2 pay cheques") — a tool
  returns them as list lengths as often as numbers, and demanding each one be
  a value would turn honest answers into fallbacks.
* **Figures from the person's own question** — "can I afford a $400 trip?"
  must be able to say $400 back.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation

__all__ = ["figures_in", "grounded_values", "ungrounded"]

_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|"
    "november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)

# Spans that are dates, removed before figures are read.
_DATES = re.compile(
    rf"""
    \b\d{{4}}-\d{{2}}(?:-\d{{2}})?\b                 # 2021-04, 2021-04-03
    | \b(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b  # March 3, Mar 3rd
    | \b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTHS})\b  # 3 March
    | \b\d{{1,2}}/\d{{1,2}}/\d{{2,4}}\b               # 03/04/2021
    """,
    re.IGNORECASE | re.VERBOSE,
)

# A figure: an optional sign, an optional currency symbol either side of it,
# digits with optional thousands separators and decimals, an optional %.
# Separators in either system: 100,000 or the Indian 1,00,000 (a lakh) — read
# as two figures, "1" and "00,000", a rupee answer could never be grounded.
_FIGURE = re.compile(
    r"""
    (?<![\w.])
    (?P<raw>
        (?P<neg>[-−])?\s?(?P<symbol>[$€£₹]|Rs\.?\s?|INR\s?)?\s?(?P<neg2>-)?
        (?P<num>\d{1,2}(?:,\d{2})+,\d{3}(?:\.\d+)?|\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)
        (?P<pct>\s?%)?
    )
    (?![\w])
    """,
    re.VERBOSE,
)

_SMALL_COUNT = 10


def _value(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def figures_in(text: str) -> list[tuple[str, Decimal]]:
    """Every figure in [text] that must be grounded, as (as written, value):
    dates, years and small counts left out."""
    scrubbed = _DATES.sub(" ", text)
    found = []
    for match in _FIGURE.finditer(scrubbed):
        value = _value(match.group("num"))
        if value is None:
            continue
        is_money = match.group("symbol") is not None
        whole = value == value.to_integral_value() and "." not in match.group("num")
        if not is_money and not match.group("pct") and whole:
            if 1900 <= value <= 2100 and "," not in match.group("num"):
                continue  # a year
            if value <= _SMALL_COUNT:
                continue  # a small count
        if match.group("neg") or match.group("neg2"):
            value = -value
        found.append((match.group("raw").strip(), value))
    return found


def grounded_values(sources: Iterable[object]) -> set[Decimal]:
    """Every value a figure may be: each number in [sources] (tool results as
    JSON text or parsed), each string that reads as a decimal, each list's
    length, and their absolute values — a tool's "-332.65" is "$332.65 less"."""
    values: set[Decimal] = set()

    def walk(item: object) -> None:
        if isinstance(item, bool) or item is None:
            return
        if isinstance(item, int | float | Decimal):
            values.add(Decimal(str(item)))
        elif isinstance(item, str):
            parsed = _value(item.strip().rstrip("%"))
            if parsed is not None:
                values.add(parsed)
        elif isinstance(item, dict):
            for nested in item.values():
                walk(nested)
        elif isinstance(item, list | tuple):
            values.add(Decimal(len(item)))
            for nested in item:
                walk(nested)

    for source in sources:
        if isinstance(source, str):
            try:
                source = json.loads(source)
            except ValueError:
                pass
        walk(source)
    return values | {abs(v) for v in values}


def ungrounded(reply: str, sources: Iterable[object], question: str = "") -> list[str]:
    """The figures in [reply] found neither in [sources] nor in [question],
    as written in the reply, in order."""
    allowed = grounded_values(sources)
    allowed |= {abs(value) for _, value in figures_in(question)}
    return [
        raw
        for raw, value in figures_in(reply)
        if value not in allowed and abs(value) not in allowed
    ]
