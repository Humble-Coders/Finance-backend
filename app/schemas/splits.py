"""`/splits` — split expense. Amounts are decimal strings, dates YYYY-MM-DD."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    # As typed: "98765 43210" or "+91 98765 43210". Read with the household's
    # region when it has no country code.
    phone: str = Field(min_length=3, max_length=32)


class ContactPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    phone: str | None = Field(default=None, min_length=3, max_length=32)


class ContactOut(BaseModel):
    id: uuid.UUID
    name: str
    # E.164.
    phone: str


class ContactsOut(BaseModel):
    contacts: list[ContactOut]


class ReceiptIn(BaseModel):
    """The receipt's text as the phone read it, card numbers and contact
    details already masked there."""

    text: str


class ReceiptItemOut(BaseModel):
    name: str
    quantity: int
    amount: str


class ReceiptOut(BaseModel):
    merchant: str | None
    occurred_on: str | None
    items: list[ReceiptItemOut]
    subtotal: str | None
    tax: str | None
    tip: str | None
    total: str | None
    # Lines the AI offered that are not printed on the receipt: left out.
    unreadable: int
    reads_left: int


class ParticipantIn(BaseModel):
    """The person themselves (`contact_id` null), or one of their contacts."""

    contact_id: uuid.UUID | None = None


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    amount: str
    # Indexes into the split's participants.
    participants: list[int] = Field(min_length=1)


class SplitIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    occurred_on: str
    # "equal" or "items"
    mode: str
    # Equal: the bill's total. Items: worked out from the items, tax and tip.
    total: str | None = None
    items: list[ItemIn] = []
    tax: str | None = None
    tip: str | None = None
    # The person themselves exactly once, and at least one other.
    participants: list[ParticipantIn] = Field(min_length=2)


class SettleIn(BaseModel):
    settled: bool


class ParticipantOut(BaseModel):
    id: uuid.UUID
    contact_id: uuid.UUID | None
    is_self: bool
    name: str
    phone: str | None
    share: str
    settled: bool
    # The items this person shares in, by name; empty for an equal split.
    items: list[str] = []


class ItemOut(BaseModel):
    name: str
    amount: str
    # Indexes into `participants`.
    participants: list[int]


class SplitOut(BaseModel):
    id: uuid.UUID
    title: str
    occurred_on: str
    mode: str
    currency: str
    total: str
    tax: str
    tip: str
    participants: list[ParticipantOut]
    items: list[ItemOut]
    # Still to come from the others.
    owed_to_you: str


class SplitSummaryOut(BaseModel):
    id: uuid.UUID
    title: str
    occurred_on: str
    total: str
    people: int
    owed_to_you: str
    settled: bool


class SplitsOut(BaseModel):
    splits: list[SplitSummaryOut]
