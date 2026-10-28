"""The AI coach's API shapes (M6)."""

from __future__ import annotations

from pydantic import BaseModel


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
