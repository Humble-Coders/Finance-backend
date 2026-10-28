"""Suggested questions for the AI coach, answered without AI (#84).

Gated by `ai_chat` only. Nothing here reaches a model, so neither the
AI-processing consent nor the no-training tier applies, and answers do not
count against the daily chat cap — those are 6.4's, for model calls.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.db import get_session
from app.schemas.chat import SuggestionAnswerOut, SuggestionOut, SuggestionsOut
from app.services import chat_templates
from app.services.capabilities import require_feature
from app.services.conflicts import log_conflict
from app.services.identity import ResolvedIdentity

FEATURE = "ai_chat"
NOTHING_TO_SAY = "nothing_to_say"

router = APIRouter(
    prefix="/chat/suggestions",
    tags=["chat"],
    dependencies=[Depends(require_feature(FEATURE))],
)


def _today() -> date:
    return datetime.now(UTC).date()


@router.get("", response_model=SuggestionsOut)
async def list_suggestions(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> SuggestionsOut:
    """The suggestions this household has something to say about, now."""
    offered = await chat_templates.available(session, identity.household, _today())
    # The budget answer reads the month's budget as the Budget tab does,
    # which may prepare it; kept, as opening the tab would keep it.
    await session.commit()
    return SuggestionsOut(
        suggestions=[SuggestionOut(id=s.id, text=s.text) for s in offered]
    )


@router.post("/{suggestion_id}/answer", response_model=SuggestionAnswerOut)
async def answer_suggestion(
    suggestion_id: str,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> SuggestionAnswerOut:
    try:
        answer = await chat_templates.answer(
            session, identity.household, suggestion_id, _today()
        )
    except chat_templates.UnknownSuggestion:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"}
        ) from None
    await session.commit()
    if answer is None:
        # Offered earlier, but the data under it has gone (a row deleted).
        log_conflict(
            NOTHING_TO_SAY,
            "suggestion_has_no_data",
            household_id=str(identity.household.id),
            suggestion_id=suggestion_id,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": NOTHING_TO_SAY,
                "message": "There's nothing to answer that with yet.",
            },
        )
    return SuggestionAnswerOut(text=answer.text, figures=list(answer.figures))
