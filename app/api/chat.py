"""The AI coach's conversations (#71).

All gated by `ai_chat`. Posting a message answers it synchronously (no
streaming in v1 — the app shows a typing indicator); `app/services/chat.py`
decides how, and refuses with the codes the app already handles. Another
household's conversation is 404, never 403: a 403 would confirm it exists.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.config import get_settings
from app.db import get_session
from app.models.derived import ChatConversation
from app.schemas.chat import (
    ConversationDetailOut,
    ConversationOut,
    ConversationsOut,
    MessageIn,
    MessageOut,
    ReplyOut,
)
from app.services import chat
from app.services.capabilities import require_feature
from app.services.identity import ResolvedIdentity

FEATURE = "ai_chat"

router = APIRouter(
    prefix="/chat/conversations",
    tags=["chat"],
    dependencies=[Depends(require_feature(FEATURE))],
)


def _today() -> date:
    return datetime.now(UTC).date()


def _summary(conversation: ChatConversation) -> ConversationOut:
    return ConversationOut(
        id=conversation.id,
        title=conversation.title,
        preview=conversation.last_message_preview,
        updated_at=conversation.updated_at,
    )


async def _owned(
    session: AsyncSession, household_id: uuid.UUID, conversation_id: uuid.UUID
) -> ChatConversation:
    conversation = await session.scalar(
        select(ChatConversation).where(
            ChatConversation.id == conversation_id,
            ChatConversation.household_id == household_id,
        )
    )
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"}
        )
    return conversation


@router.get("", response_model=ConversationsOut)
async def list_conversations(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> ConversationsOut:
    """Newest first. A year untouched and a conversation is gone — removed
    here, when the list is opened."""
    household_id = identity.household.id
    await chat.forget_untouched(session, household_id, datetime.now(UTC))
    result = await session.execute(
        select(ChatConversation)
        .where(ChatConversation.household_id == household_id)
        .order_by(
            ChatConversation.updated_at.desc(), ChatConversation.created_at.desc()
        )
    )
    await session.commit()
    return ConversationsOut(conversations=[_summary(c) for c in result.scalars().all()])


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def start_conversation(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> ConversationOut:
    conversation = ChatConversation(household_id=identity.household.id, messages=[])
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return _summary(conversation)


@router.get("/{conversation_id}", response_model=ConversationDetailOut)
async def read_conversation(
    conversation_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> ConversationDetailOut:
    conversation = await _owned(session, identity.household.id, conversation_id)
    return ConversationDetailOut(
        id=conversation.id,
        title=conversation.title,
        messages=[
            MessageOut(
                role=m["role"],
                text=m["text"],
                at=m["at"],
                answered_by=m.get("answered_by"),
                disclaimer=m.get("disclaimer"),
            )
            for m in conversation.messages or []
        ],
    )


@router.post("/{conversation_id}/messages", response_model=ReplyOut)
async def post_message(
    conversation_id: uuid.UUID,
    body: MessageIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> ReplyOut:
    conversation = await _owned(session, identity.household.id, conversation_id)
    try:
        reply = await chat.post_message(
            session,
            identity,
            conversation,
            body.text.strip(),
            body.suggestion_id,
            settings=get_settings(),
            today=_today(),
        )
    except chat.ChatRefused as refusal:
        # The 409 was logged (log_conflict) where it was decided. What the
        # model run spent so far still counts — commit it.
        await session.commit()
        raise HTTPException(
            status_code=refusal.status,
            detail={"code": refusal.code, "message": refusal.message, **refusal.extra},
        ) from None
    await session.commit()
    return ReplyOut(
        text=reply.text, disclaimer=reply.disclaimer, answered_by=reply.answered_by
    )


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> Response:
    conversation = await _owned(session, identity.household.id, conversation_id)
    await session.delete(conversation)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
