"""Request and response shapes for household categories."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["CategoryIn", "CategoryOut"]


class CategoryIn(BaseModel):
    """A category the household wants. The name is all it may choose.

    Unknown fields are refused, not ignored. `household_id` is never read from
    a request — it comes from the caller's token — so ignoring one would be
    safe; refusing it is also *visible*. A client sending `household_id: null`
    or `is_system: true` has misunderstood something, and a quiet success would
    let it go on misunderstanding.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)


class CategoryOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    is_system: bool
