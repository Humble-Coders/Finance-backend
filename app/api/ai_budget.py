"""`/budgets/ai` — the AI's budget suggestions (product decision, 2026-10-25).

* `POST /budgets/ai/setup` — a first budget from setup's answers, made at
  most three times. `GET` returns the one made last.
* `POST /budgets/ai/setup/accept` — the lines as the person edited them,
  written as their own: hand-set lines carry into the months after.
* `POST /budgets/{month}/ai/review` — the month's review, once a month: a
  suggestion per line from how the months before went, and advice. It answers
  with the whole budget, the suggestions on its lines.

Gated like statement import, in the same order: the `auto_budget` feature,
then a no-training API tier in production, then the person's consent to AI
processing. Every figure is the server's (`app/services/ai_budget.py`).
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import budgets as budget_api
from app.api.deps import current_identity
from app.config import get_settings
from app.core.money import MoneyError, from_minor_units, to_minor_units
from app.db import get_session
from app.models.categorization import Category
from app.models.derived import AiBudgetSuggestion
from app.models.identity import User
from app.schemas.budget import (
    AiAcceptIn,
    AiProposalLineOut,
    AiProposalOut,
    BudgetOut,
)
from app.services import ai_budget
from app.services import budget as budget_service
from app.services.ai_consent import CONSENT_REQUIRED, current_policy, has_consented
from app.services.capabilities import currency_for, require_feature
from app.services.conflicts import log_conflict
from app.services.identity import ResolvedIdentity
from app.services.learning import learning_state
from app.services.llm import LlmError, build_client, close_client

log = structlog.get_logger()

TIER_NOT_CONFIRMED = "ai_processing_unavailable"
AI_UNAVAILABLE = "ai_unavailable"
LIMIT_REACHED = "ai_budget_limit_reached"
NO_INCOME = "setup_income_missing"
STILL_LEARNING = "still_learning"
NOT_THIS_MONTH = "not_current_month"

router = APIRouter(
    prefix="/budgets",
    tags=["budgets"],
    dependencies=[Depends(require_feature(budget_api.FEATURE))],
)


def _refused(code: int, kind: str, message: str, **extra) -> HTTPException:
    return HTTPException(
        status_code=code, detail={"code": kind, "message": message, **extra}
    )


async def _may_ask(session: AsyncSession, user: User) -> None:
    """The two gates statement import has, before any model call."""
    settings = get_settings()
    if settings.is_production and not settings.llm_no_training_tier:
        log.error("llm_tier_not_confirmed", provider=settings.llm_provider)
        raise _refused(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            TIER_NOT_CONFIRMED,
            "AI suggestions are temporarily unavailable.",
        )
    policy = await current_policy(session)
    if policy is None or not await has_consented(session, user, policy):
        log_conflict(
            CONSENT_REQUIRED,
            "no_ai_policy_configured" if policy is None else "consent_not_given",
            user_id=str(user.id),
        )
        raise _refused(
            status.HTTP_409_CONFLICT,
            CONSENT_REQUIRED,
            "Consent to AI processing is needed for AI suggestions.",
            policy_version=policy.version if policy else None,
        )


def _unavailable() -> HTTPException:
    return _refused(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        AI_UNAVAILABLE,
        "The AI could not make a suggestion just now. Try again in a moment.",
    )


async def _proposal(
    session: AsyncSession, row: AiBudgetSuggestion, currency: str, income: int
) -> AiProposalOut:
    ids = [uuid.UUID(r["category_id"]) for r in row.lines]
    names = {
        c.id: c.name
        for c in await session.scalars(select(Category).where(Category.id.in_(ids)))
    }
    needs, wants = ai_budget.totals(row.lines)
    savings = row.savings_minor_units or 0

    def money(minor: int) -> str:
        return from_minor_units(minor, currency)

    def share(minor: int) -> int:
        return round(minor * 100 / income) if income else 0

    return AiProposalOut(
        month=row.month,
        currency=currency,
        income=money(income),
        lines=[
            AiProposalLineOut(
                category_id=uuid.UUID(r["category_id"]),
                slug=r["slug"],
                name=names.get(uuid.UUID(r["category_id"]), r["slug"]),
                kind="debt"
                if r["slug"] == ai_budget.DEBT_SLUG
                else ai_budget.kind_of(r["slug"]),
                amount=money(r["amount_minor_units"]),
                committed=money(r.get("committed_minor_units", 0)),
                reason=r.get("reason"),
            )
            for r in row.lines
        ],
        savings=money(savings),
        needs=money(needs),
        wants=money(wants),
        needs_percent=share(needs),
        wants_percent=share(wants),
        savings_percent=share(savings),
        note=row.advice,
        attempts_left=max(ai_budget.MAX_SETUP_ATTEMPTS - row.attempts, 0),
    )


async def _income(session: AsyncSession, household_id: uuid.UUID, currency: str) -> int:
    return await budget_service._expected_income(session, household_id, currency)


@router.post("/ai/setup", response_model=AiProposalOut)
async def suggest_setup(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> AiProposalOut:
    """A first budget from setup's answers. Each call makes a new one, up to
    three; then 429, and `GET` still returns the last."""
    household = identity.household
    currency = await currency_for(session, household)
    await _may_ask(session, identity.user)
    today = budget_api._today()
    client = None
    try:
        client = build_client(get_settings())
        row, _ = await ai_budget.suggest_setup(
            session, client, household.id, currency, today
        )
    except ai_budget.NoIncome as error:
        log_conflict(NO_INCOME, "no_setup_income", household_id=str(household.id))
        raise _refused(
            status.HTTP_409_CONFLICT,
            NO_INCOME,
            "Add your monthly income in setup first.",
        ) from error
    except ai_budget.TooManyAttempts as error:
        raise _refused(
            status.HTTP_429_TOO_MANY_REQUESTS,
            LIMIT_REACHED,
            "That's all the suggestions for now. Edit the last one to fit.",
        ) from error
    except LlmError as error:
        log.warning("ai_budget_failed", kind=ai_budget.SETUP, reason=str(error))
        raise _unavailable() from error
    finally:
        if client is not None:
            await close_client(client)
    await session.commit()
    return await _proposal(
        session, row, currency, await _income(session, household.id, currency)
    )


@router.get("/ai/setup", response_model=AiProposalOut)
async def read_setup(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> AiProposalOut:
    """The setup suggestion made last this month, or 404."""
    household = identity.household
    currency = await currency_for(session, household)
    month = budget_api._today().replace(day=1)
    row = await ai_budget.stored(session, household.id, month, ai_budget.SETUP)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"}
        )
    return await _proposal(
        session, row, currency, await _income(session, household.id, currency)
    )


def _amount(raw: str, currency: str, field: str) -> int:
    try:
        amount = to_minor_units(raw, currency)
    except MoneyError as error:
        raise _refused(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_amount",
            str(error),
            field=field,
        ) from error
    if amount < 0 or amount > budget_api.MAX_LINE_MINOR_UNITS:
        raise _refused(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_amount",
            "That amount is not a budget line.",
            field=field,
        )
    return amount


@router.post("/ai/setup/accept", response_model=BudgetOut)
async def accept_setup(
    body: AiAcceptIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> BudgetOut:
    """Start the budget from the lines the person kept. No model is asked:
    these are the person's own figures now."""
    household = identity.household
    currency = await currency_for(session, household)
    today = budget_api._today()
    ids = [line.category_id for line in body.lines]
    visible = {
        c.id: c
        for c in await session.scalars(
            select(Category).where(
                Category.id.in_(ids),
                or_(
                    Category.household_id.is_(None),
                    Category.household_id == household.id,
                ),
            )
        )
    }
    items = []
    for i, line in enumerate(body.lines):
        category = visible.get(line.category_id)
        if category is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found", "field": f"lines.{i}.category_id"},
            )
        if category.slug == ai_budget.DEBT_SLUG:
            continue  # the Debt line follows the minimums, by its own rule
        items.append((category, _amount(line.amount, currency, f"lines.{i}.amount")))
    savings = (
        _amount(body.savings, currency, "savings")
        if body.savings is not None and body.savings.strip()
        else None
    )
    learning = await learning_state(session, household.id, currency, today)
    try:
        view = await budget_service.apply_lines(
            session,
            household.id,
            currency,
            today.replace(day=1),
            today,
            items,
            savings,
            ready=learning.ready,
        )
    except budget_service.NotBudgetable as error:
        raise budget_api._not_budgetable(str(error)) from error
    await session.commit()
    return budget_api._out(view, learning)


@router.post("/{month}/ai/review", response_model=BudgetOut)
async def review(
    month: str,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> BudgetOut:
    """This month's AI review: made once, then returned as stored."""
    when = budget_api._month(month)
    today = budget_api._today()
    if when != today.replace(day=1):
        raise _refused(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            NOT_THIS_MONTH,
            "Only the month you are in can be reviewed.",
        )
    household = identity.household
    currency = await currency_for(session, household)
    learning = await learning_state(session, household.id, currency, today)
    if not learning.ready:
        log_conflict(
            STILL_LEARNING, "review_before_ready", household_id=str(household.id)
        )
        raise _refused(
            status.HTTP_409_CONFLICT,
            STILL_LEARNING,
            "The AI reviews your budget once there is a full month to learn from.",
        )
    existing = await ai_budget.stored(session, household.id, when, ai_budget.REVIEW)
    if existing is None:
        await _may_ask(session, identity.user)
        client = None
        try:
            client = build_client(get_settings())
            await ai_budget.review(session, client, household.id, currency, today)
        except ai_budget.NoIncome as error:
            log_conflict(NO_INCOME, "no_setup_income", household_id=str(household.id))
            raise _refused(
                status.HTTP_409_CONFLICT,
                NO_INCOME,
                "Add your monthly income in setup first.",
            ) from error
        except LlmError as error:
            log.warning("ai_budget_failed", kind=ai_budget.REVIEW, reason=str(error))
            raise _unavailable() from error
        finally:
            if client is not None:
                await close_client(client)
    view = await budget_service.budget_for(
        session, household.id, currency, when, today, ready=True
    )
    await session.commit()
    return budget_api._out(view, learning)
