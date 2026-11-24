"""Checking each row's direction against the statement's running balance.

The model decides whether a row is money in or money out, and on a statement
read as text it is often guessing: a table's Withdrawals and Deposits columns
flatten into one line, the empty cell disappears, and `450.00` could have been
under either. A wrong guess puts a purchase into income — the commonest import
complaint there is.

Most bank statements print the running balance on every row, and that settles
it without guessing: if the balance fell by exactly the row's amount, the money
went out; if it rose, it came in. So the model is asked to copy each row's
balance (checked against the text like any amount), and this module does the
arithmetic and corrects the direction where the two disagree.

**Only exact matches count.** A pair of rows whose balances differ by anything
but that row's amount — a row the model missed between them, a balance misread,
the first row of the statement — is left exactly as the model read it. This
never invents a direction; it only replaces a guess with a sum.

**Both orders.** Statements list oldest first or newest first. Each is tried
and the one under which more rows add up is used.
"""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from app.models.enums import AccountKind, TransactionDirection

if TYPE_CHECKING:
    from app.services.statements import ParsedRow

__all__ = ["owed_balance", "reconcile", "signed_balance"]

# Accounts whose statement balance is what is OWED: it rises with a purchase.
_OWED = {AccountKind.credit_card, AccountKind.loan}

_MARKER = re.compile(r"(?i)\b(cr|dr)\.?\s*$")
_NEGATIVE_START = ("-", "−", "–", "(")
_NEGATIVE_END = ("-", "−", "–", ")")


def owed_balance(kind: AccountKind | None) -> bool:
    """Whether this account's balance counts what is owed rather than held."""
    return kind in _OWED


def signed_balance(printed: str, bare: str, kind: AccountKind | None) -> Decimal | None:
    """A balance as printed, as a signed number in the account's own terms.

    `printed` is what the statement shows (`1,204.55 Cr`, `(12.00)`, `-8.10`);
    `bare` is its digits, already normalised. A held-money account reads `DR`
    as overdrawn; an owed-money account reads `CR` as a credit balance. A minus
    or brackets is negative either way.
    """
    try:
        value = Decimal(bare)
    except (InvalidOperation, ValueError):
        return None
    text = printed.strip()
    marker = _MARKER.search(text)
    if marker is not None:
        text = text[: marker.start()].strip()
        word = marker.group(1).lower()
        negative = word == ("cr" if owed_balance(kind) else "dr")
        return -value if negative else value
    if text.startswith(_NEGATIVE_START) or text.endswith(_NEGATIVE_END):
        return -value
    return value


def _direction(change: Decimal, owed: bool) -> TransactionDirection:
    """Which way the money went, given how the balance moved."""
    rose = change > 0
    # Held money rises with a deposit; an owed balance rises with a purchase.
    if owed:
        return TransactionDirection.debit if rose else TransactionDirection.credit
    return TransactionDirection.credit if rose else TransactionDirection.debit


def reconcile(
    rows: list[ParsedRow], kind: AccountKind | None
) -> tuple[list[ParsedRow], int]:
    """The rows with any direction the balances disprove corrected.

    Returns the rows and how many directions changed. Rows without a balance,
    and rows whose balances do not add up exactly, are returned unchanged.
    """
    if kind is None or len(rows) < 2:
        return rows, 0
    owed = owed_balance(kind)
    balances = [row.balance for row in rows]
    # A card statement occasionally prints what is owed as a negative figure.
    # Read that way, every purchase would look like a payment; mostly-negative
    # balances on an owed account mean the sign is that statement's convention.
    if owed:
        known = [b for b in balances if b is not None]
        if known and sum(1 for b in known if b < 0) * 2 > len(known):
            balances = [None if b is None else -b for b in balances]
    amounts = [Decimal(row.amount) for row in rows]

    def forward(i: int) -> TransactionDirection | None:
        """Oldest first: row i is what moved the balance from row i-1's."""
        before, after = balances[i - 1], balances[i]
        if before is None or after is None:
            return None
        change = after - before
        return (
            _direction(change, owed)
            if change != 0 and abs(change) == amounts[i]
            else None
        )

    def backward(i: int) -> TransactionDirection | None:
        """Newest first: row i is what moved the balance from row i+1's."""
        before, after = balances[i + 1], balances[i]
        if before is None or after is None:
            return None
        change = after - before
        return (
            _direction(change, owed)
            if change != 0 and abs(change) == amounts[i]
            else None
        )

    oldest_first = {i: d for i in range(1, len(rows)) if (d := forward(i)) is not None}
    newest_first = {
        i: d for i in range(len(rows) - 1) if (d := backward(i)) is not None
    }
    proven = oldest_first if len(oldest_first) >= len(newest_first) else newest_first
    if not proven:
        return rows, 0

    corrected = 0
    out = list(rows)
    for i, direction in proven.items():
        if out[i].direction != direction:
            out[i] = replace(out[i], direction=direction)
            corrected += 1
    return out, corrected
