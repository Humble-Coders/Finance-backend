"""The AI coach's API shapes (M6)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.schemas.budget import LearningOut


class SuggestionOut(BaseModel):
    """A question the coach can answer now, offered as a chip (#84)."""

    id: str
    text: str


class SuggestionsOut(BaseModel):
    suggestions: list[SuggestionOut]


class SuggestionAnswerOut(BaseModel):
    """A suggested question's answer, written by the server — no model.

    `figures` holds every amount in `text` as a plain decimal string ("1234.50"),
    in the order the text states them, so a client or a grounding check can
    rely on them without reading the sentence.
    """

    text: str
    figures: list[str]


# --- Conversations (#71) ----------------------------------------------------------

MAX_MESSAGE_CHARS = 1_000


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str | None
    preview: str | None
    updated_at: datetime


class ConversationsOut(BaseModel):
    conversations: list[ConversationOut]


class MessageOut(BaseModel):
    """A turn as the person sees it — text only. The tools behind an answer
    stay on the server."""

    role: str
    text: str
    at: datetime
    # Assistant turns only.
    answered_by: str | None = None
    disclaimer: bool | None = None


class ConversationDetailOut(BaseModel):
    id: uuid.UUID
    title: str | None
    messages: list[MessageOut]


class MessageIn(BaseModel):
    """A question in the person's words, or a suggestion chip tapped
    (`suggestion_id`, from `GET /chat/suggestions`) — then `text` may be empty."""

    text: str = Field(default="", max_length=MAX_MESSAGE_CHARS)
    suggestion_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _something_to_answer(self) -> MessageIn:
        if not self.text.strip() and self.suggestion_id is None:
            raise ValueError("send text or a suggestion_id")
        return self


class ReplyOut(BaseModel):
    text: str
    # True when the answer is about what is to come: show /legal/disclaimer.
    disclaimer: bool
    # template | cache | model — how it was answered.
    answered_by: str
    # While the household is still learning (#72), its progress — the same
    # shape the budget and the score return; null once it is ready.
    learning: LearningOut | None = None
