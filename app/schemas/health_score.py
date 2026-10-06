"""The health score payload (ticket #57).

Money in a component's `inputs` crosses as decimal strings, as everywhere
else. Component scores are whole numbers for display; the overall score is
computed from their exact values, so it can differ by one from a weighted
average of the rounded parts.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel

from app.schemas.budget import LearningOut


class ComponentOut(BaseModel):
    key: str
    # Null when the component could not be scored.
    score: int | None
    # The share of the score this component carried after renormalising, as
    # a percentage to two places ("61.54"); "0.00" when unavailable.
    weight: str
    available: bool
    # What the component was scored from: amounts as decimal strings.
    inputs: dict[str, Any]


class SnapshotOut(BaseModel):
    scored_on: date
    score: int
    formula_version: str


class HealthScoreOut(BaseModel):
    status: Literal["learning", "ready"]
    # Present only while learning.
    learning: LearningOut | None = None
    # Null while learning, or when no component can be scored.
    score: int | None = None
    formula_version: str | None = None
    components: list[ComponentOut] = []
    # Up to the last 12 snapshots, oldest first.
    history: list[SnapshotOut] = []
