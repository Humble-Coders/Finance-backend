"""Whether there is enough history to say anything yet (PRD F8).

A budget built from a week of spending, or a health score from a dozen rows,
is a number stated with confidence it has not earned. Until the household has
a complete calendar month and enough transactions to describe it, the honest
answer is "still learning", with how far along it is.

One function so the budget (4.1), the health score (4.2) and the dashboard
(4.3) cannot each draw the line in a slightly different place.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.money import Transaction
from app.services.dashboard import countable

# Manager decision, 2026-10-04 (ticket #56).
NEEDS_COMPLETE_MONTHS = 1
NEEDS_TRANSACTIONS = 20


@dataclass(frozen=True)
class LearningState:
    # Calendar months that have ended and hold at least one countable row.
    complete_months: int
    # Countable rows, ever.
    transactions: int
    needs: dict[str, int] = field(
        default_factory=lambda: {
            "complete_months": NEEDS_COMPLETE_MONTHS,
            "transactions": NEEDS_TRANSACTIONS,
        }
    )

    @property
    def ready(self) -> bool:
        return (
            self.complete_months >= NEEDS_COMPLETE_MONTHS
            and self.transactions >= NEEDS_TRANSACTIONS
        )


async def learning_state(
    session: AsyncSession, household_id: uuid.UUID, currency: str, today: date
) -> LearningState:
    """How much countable history the household has, as of [today].

    A month is complete once it has ended, not once it is full: a statement
    that starts on the 20th still describes a month the household lived
    through. The month [today] falls in never counts — it is still running.
    """
    current_month = today.replace(day=1)
    bucket = func.date_trunc("month", Transaction.occurred_on)
    transactions, complete_months = (
        await session.execute(
            select(
                func.count(Transaction.id),
                func.count(func.distinct(bucket)).filter(
                    Transaction.occurred_on < current_month
                ),
            ).where(*countable(household_id, currency))
        )
    ).one()
    return LearningState(
        complete_months=int(complete_months), transactions=int(transactions)
    )
