"""The dashboard payload.

Money crosses as decimal strings, as everywhere else: minor units are an
internal representation and a float would quietly lose a cent.

Expectations and actuals are separate fields on purpose. There is no field
anywhere in here holding the two added together, because adding them is the
double count the whole design avoids — see `app/services/dashboard.py`.
"""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel


class FlowOut(BaseModel):
    """A month's movement, and what the wizard said to expect of it."""

    actual: str
    # Null until the wizard has been filled in. A client showing "of X
    # expected" has to hide that half rather than print "of 0".
    expected: str | None = None


class StockOut(BaseModel):
    """A balance the user maintains, and this month's movement against it."""

    # From the setup wizard, and only from there: we never observe a
    # portfolio's market value or a loan's outstanding principal.
    balance: str
    # Observed — debits filed under savings or debt_payment this month.
    moved: str


class MatchOut(BaseModel):
    id: uuid.UUID
    occurred_on: date
    amount: str
    description: str | None = None


class CommitmentOut(BaseModel):
    """One obligation from setup, and the payment that settles it.

    `match` being null means **not seen this month**, which is not the same as
    unpaid: a commitment settled in cash, from another account, or written
    differently by the bank will not be found. Clients must word it that way.
    """

    name: str
    expected: str
    match: MatchOut | None = None


class MonthPointOut(BaseModel):
    month: date
    # Null for a month with no rows. A zero here would be a fact nobody
    # observed, and a chart must not draw one.
    net: str | None = None


class DayPointOut(BaseModel):
    day: date
    # The running balance at the end of the day: in minus out since the first
    # of the month. The last one equals `net`.
    net: str


class DashboardOut(BaseModel):
    month: date
    currency: str

    # The hero: income minus expenses. Commitments are already inside expenses
    # wherever the statement shows them.
    net: str
    # Null when the previous month has no rows, so the client omits the
    # comparison instead of claiming an infinite rise from nothing.
    previous_net: str | None = None

    income: FlowOut
    expenses: FlowOut
    investments: StockOut
    debts: StockOut

    commitments: list[CommitmentOut]
    trend: list[MonthPointOut]
    # The month day by day, for the home chart; empty for a month with no
    # rows. Defaulted so an older client that never reads it is unaffected.
    daily: list[DayPointOut] = []

    # Rows in this month still waiting on a person. The figures above exclude
    # unresolved suspected duplicates, so this is also the honest caveat on
    # them.
    pending_review: int
