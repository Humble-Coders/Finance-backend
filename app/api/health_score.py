"""`GET /health-score` — the Money Health Score and its history (PRD F6).

Gated by `health_score`. Computed on read and kept as today's snapshot; see
`app/services/health_score.py`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.core.money import from_minor_units
from app.db import get_session
from app.schemas.budget import LearningNeedsOut, LearningOut
from app.schemas.health_score import ComponentOut, HealthScoreOut, SnapshotOut
from app.services import health_score as service
from app.services.capabilities import currency_for, require_feature
from app.services.identity import ResolvedIdentity

FEATURE = "health_score"

router = APIRouter(
    tags=["health-score"],
    dependencies=[Depends(require_feature(FEATURE))],
)


def _today() -> date:
    """UTC, the convention `/dashboard` and `/budgets` use."""
    return datetime.now(UTC).date()


def _inputs(key: str, inputs: service.ScoreInputs, currency: str) -> dict:
    """What [key] was scored from, money as decimal strings."""

    def money(minor: int) -> str:
        return from_minor_units(minor, currency)

    if key == service.SAVINGS_CONSISTENCY:
        return {
            "months": [
                {
                    "month": m.month.isoformat(),
                    "income": money(m.income),
                    "expenses": money(m.expenses),
                    "net": money(m.net),
                }
                for m in inputs.months
            ]
        }
    if key == service.SPENDING_VS_BUDGET:
        return {
            "lines": [
                {
                    "slug": line.slug,
                    "allocated": money(line.allocated),
                    "spent": money(line.spent),
                }
                for line in inputs.budget_lines
            ]
        }
    debt = inputs.debt
    return {
        "debts": debt.debts,
        "debts_with_minimum": debt.debts_with_minimum,
        "required": money(debt.required),
        "paid": money(debt.paid),
    }


@router.get("/health-score", response_model=HealthScoreOut)
async def read_health_score(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> HealthScoreOut:
    """The score, how it was reached, and how it has moved.

    "Still learning" is a 200 that writes nothing: no snapshot exists before
    there is enough history to score.
    """
    household = identity.household
    currency = await currency_for(session, household)
    current = await service.current_score(session, household.id, currency, _today())
    learning = current.learning
    if current.result is None or current.inputs is None:
        return HealthScoreOut(
            status="learning",
            learning=LearningOut(
                ready=False,
                complete_months=learning.complete_months,
                transactions=learning.transactions,
                needs=LearningNeedsOut(**learning.needs),
            ),
        )
    await session.commit()
    result = current.result
    return HealthScoreOut(
        status="ready",
        score=result.score,
        formula_version=result.formula_version,
        components=[
            ComponentOut(
                key=c.key,
                score=c.display_score,
                weight=str(c.weight.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                available=c.available,
                inputs=_inputs(c.key, current.inputs, currency),
            )
            for c in result.components
        ],
        history=[
            SnapshotOut(
                scored_on=snap.scored_on,
                score=snap.score,
                formula_version=snap.formula_version,
            )
            for snap in current.history
        ],
    )
