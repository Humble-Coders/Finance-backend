"""`/splits` — split expense, gated by `split_expenses`.

A bill divided between the person and their contacts, the shares worked out
here (`services.splits`), and each sent on by the app as a WhatsApp message
the person sends themselves. Nothing here touches the ledger: the bill reaches
it through the statement it was paid on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import phonenumbers
import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api import ai_budget as ai_budget_api
from app.api.deps import current_identity
from app.config import get_settings
from app.core.money import MoneyError, from_minor_units, to_minor_units
from app.db import get_session
from app.models.splits import (
    Split,
    SplitContact,
    SplitItem,
    SplitItemShare,
    SplitParticipant,
    SplitReceiptUsage,
)
from app.schemas.splits import (
    ContactIn,
    ContactOut,
    ContactPatch,
    ContactsOut,
    ItemOut,
    ParticipantOut,
    ReceiptIn,
    ReceiptItemOut,
    ReceiptOut,
    SettleIn,
    SplitIn,
    SplitOut,
    SplitsOut,
    SplitSummaryOut,
)
from app.services import splits as service
from app.services.capabilities import currency_for, require_feature
from app.services.conflicts import log_conflict
from app.services.identity import ResolvedIdentity
from app.services.llm import LlmError, build_client

log = structlog.get_logger()

FEATURE = "split_expenses"
MAX_PEOPLE = 20
MAX_CONTACTS = 300

router = APIRouter(
    prefix="/splits",
    tags=["splits"],
    dependencies=[Depends(require_feature(FEATURE))],
)


def _today() -> date:
    return datetime.now(UTC).date()


def _unprocessable(code: str, field: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": code, "field": field, "message": message},
    )


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"}
    )


# --- Contacts --------------------------------------------------------------------


def _phone(raw: str, region: str | None) -> str:
    """[raw] as E.164, read with the household's region when it has no
    country code; 422 when it is not a number WhatsApp could reach."""
    try:
        number = phonenumbers.parse(raw, region or None)
    except phonenumbers.NumberParseException as error:
        raise _unprocessable(
            "invalid_phone", "phone", "That isn't a phone number."
        ) from error
    if not phonenumbers.is_valid_number(number):
        raise _unprocessable("invalid_phone", "phone", "That isn't a phone number.")
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def _contact_out(contact: SplitContact) -> ContactOut:
    return ContactOut(id=contact.id, name=contact.name, phone=contact.phone)


@router.get("/contacts", response_model=ContactsOut)
async def list_contacts(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> ContactsOut:
    rows = await session.scalars(
        select(SplitContact)
        .where(SplitContact.household_id == identity.household.id)
        .order_by(func.lower(SplitContact.name), SplitContact.id)
    )
    return ContactsOut(contacts=[_contact_out(c) for c in rows])


@router.post(
    "/contacts", response_model=ContactOut, status_code=status.HTTP_201_CREATED
)
async def add_contact(
    body: ContactIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> ContactOut:
    """Save someone to split with. The same number again updates the name
    rather than making a second entry."""
    household = identity.household
    phone = _phone(body.phone, household.country_code)
    name = body.name.strip()
    count = await session.scalar(
        select(func.count()).where(SplitContact.household_id == household.id)
    )
    existing = await session.scalar(
        select(SplitContact).where(
            SplitContact.household_id == household.id, SplitContact.phone == phone
        )
    )
    if existing is None and (count or 0) >= MAX_CONTACTS:
        log_conflict(
            "contact_limit_reached",
            "split_contacts_full",
            household_id=str(household.id),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "contact_limit_reached", "limit": MAX_CONTACTS},
        )
    if existing is not None:
        existing.name = name
        contact = existing
    else:
        contact = SplitContact(household_id=household.id, name=name, phone=phone)
        session.add(contact)
    await session.flush()
    await session.commit()
    return _contact_out(contact)


async def _owned_contact(
    session: AsyncSession, household_id: uuid.UUID, contact_id: uuid.UUID
) -> SplitContact:
    contact = await session.get(SplitContact, contact_id)
    if contact is None or contact.household_id != household_id:
        raise _not_found()
    return contact


@router.patch("/contacts/{contact_id}", response_model=ContactOut)
async def edit_contact(
    contact_id: uuid.UUID,
    body: ContactPatch,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> ContactOut:
    household = identity.household
    contact = await _owned_contact(session, household.id, contact_id)
    if body.name is not None:
        contact.name = body.name.strip()
    if body.phone is not None:
        phone = _phone(body.phone, household.country_code)
        taken = await session.scalar(
            select(SplitContact.id).where(
                SplitContact.household_id == household.id,
                SplitContact.phone == phone,
                SplitContact.id != contact.id,
            )
        )
        if taken is not None:
            log_conflict(
                "contact_exists",
                "split_contact_number_taken",
                household_id=str(household.id),
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "contact_exists",
                    "message": "Someone has that number.",
                },
            )
        contact.phone = phone
    await session.commit()
    return _contact_out(contact)


@router.delete("/contacts/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contact(
    contact_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Gone from the address book. Bills already split keep their own copy."""
    contact = await _owned_contact(session, identity.household.id, contact_id)
    await session.delete(contact)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Reading a receipt -------------------------------------------------------------


@router.post("/receipt", response_model=ReceiptOut)
async def read_receipt(
    body: ReceiptIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> ReceiptOut:
    """The receipt's items, tax, tip and total, read by the AI from text the
    phone already masked. Every amount is one printed on the receipt.

    The same gates as any model call (`ai_budget._may_ask`): consent to AI
    processing (409 `consent_required`), the provider confirmed (503), and a
    monthly number of reads (429 `receipt_limit_reached`)."""
    household = identity.household
    if len(body.text) > service.MAX_RECEIPT_CHARS:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": "receipt_too_long",
                "limit_chars": service.MAX_RECEIPT_CHARS,
            },
        )
    await ai_budget_api._may_ask(session, identity.user)
    month = _today().replace(day=1)
    used = await session.scalar(
        select(SplitReceiptUsage.reads).where(
            SplitReceiptUsage.household_id == household.id,
            SplitReceiptUsage.month == month,
        )
    )
    if (used or 0) >= service.MAX_RECEIPT_READS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "receipt_limit_reached",
                "message": "That's this month's receipts. Type the bill in instead.",
                "limit": service.MAX_RECEIPT_READS,
            },
        )
    currency = await currency_for(session, household)
    client = None
    try:
        # Inside the try: a missing key or unknown provider raises `LlmError`
        # here too, and must be the 503 below rather than a 500.
        client = build_client(get_settings())
        read = await service.read_receipt(client, body.text, currency)
    except LlmError as error:
        log.warning("receipt_read_failed", error=str(error))
        raise ai_budget_api._unavailable() from error
    finally:
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()
    await session.execute(
        pg_insert(SplitReceiptUsage)
        .values(id=uuid.uuid4(), household_id=household.id, month=month, reads=1)
        .on_conflict_do_update(
            index_elements=[SplitReceiptUsage.household_id, SplitReceiptUsage.month],
            set_={"reads": SplitReceiptUsage.reads + 1},
        )
    )
    await session.commit()
    return ReceiptOut(
        merchant=read.merchant,
        occurred_on=read.occurred_on.isoformat() if read.occurred_on else None,
        items=[
            ReceiptItemOut(name=i.name, quantity=i.quantity, amount=i.amount)
            for i in read.items
        ],
        subtotal=read.subtotal,
        tax=read.tax,
        tip=read.tip,
        total=read.total,
        unreadable=read.unreadable,
        reads_left=max(service.MAX_RECEIPT_READS - (used or 0) - 1, 0),
    )


# --- Splits ------------------------------------------------------------------------


def _money(raw: str | None, currency: str, field: str, *, positive: bool) -> int:
    if raw is None or not str(raw).strip():
        if positive:
            raise _unprocessable("invalid_amount", field, "An amount is needed.")
        return 0
    try:
        value = to_minor_units(raw, currency)
    except MoneyError as error:
        raise _unprocessable("invalid_amount", field, str(error)) from error
    if value < 0 or (positive and value == 0):
        raise _unprocessable("invalid_amount", field, "That amount can't be used.")
    return value


def _split_out(split: Split) -> SplitOut:
    currency = split.currency
    ids = {p.id: i for i, p in enumerate(split.participants)}
    item_names: dict[uuid.UUID, list[str]] = {p.id: [] for p in split.participants}
    items = []
    for item in split.items:
        who = sorted(
            ids[s.participant_id] for s in item.shares if s.participant_id in ids
        )
        for share in item.shares:
            item_names.setdefault(share.participant_id, []).append(item.name)
        items.append(
            ItemOut(
                name=item.name,
                amount=from_minor_units(item.amount_minor_units, currency),
                participants=who,
            )
        )
    owed = sum(
        p.share_minor_units
        for p in split.participants
        if not p.is_self and p.settled_at is None
    )
    return SplitOut(
        id=split.id,
        title=split.title,
        occurred_on=split.occurred_on.isoformat(),
        mode=split.mode,
        currency=currency,
        total=from_minor_units(split.total_minor_units, currency),
        tax=from_minor_units(split.tax_minor_units, currency),
        tip=from_minor_units(split.tip_minor_units, currency),
        participants=[
            ParticipantOut(
                id=p.id,
                contact_id=p.contact_id,
                is_self=p.is_self,
                name=p.name,
                phone=p.phone,
                share=from_minor_units(p.share_minor_units, currency),
                settled=p.is_self or p.settled_at is not None,
                items=item_names.get(p.id, []),
            )
            for p in split.participants
        ],
        items=items,
        owed_to_you=from_minor_units(owed, currency),
    )


async def _loaded(
    session: AsyncSession, household_id: uuid.UUID, split_id: uuid.UUID
) -> Split:
    split = await session.scalar(
        select(Split)
        .where(Split.id == split_id, Split.household_id == household_id)
        .options(selectinload(Split.participants), selectinload(Split.items))
        .execution_options(populate_existing=True)
    )
    if split is None:
        raise _not_found()
    return split


@router.post("", response_model=SplitOut, status_code=status.HTTP_201_CREATED)
async def create_split(
    body: SplitIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> SplitOut:
    """Divide a bill. The shares are worked out here and add up to the bill
    exactly; the app's preview is worked out the same way."""
    household, user = identity.household, identity.user
    currency = await currency_for(session, household)
    if body.mode not in ("equal", "items"):
        raise _unprocessable("invalid_mode", "mode", "Split equally or by items.")
    try:
        occurred_on = date.fromisoformat(body.occurred_on)
    except ValueError as error:
        raise _unprocessable(
            "invalid_date", "occurred_on", "That isn't a date."
        ) from error

    selves = [p for p in body.participants if p.contact_id is None]
    if len(selves) != 1:
        raise _unprocessable(
            "invalid_participants", "participants", "You are in every split, once."
        )
    if len(body.participants) > MAX_PEOPLE:
        raise _unprocessable(
            "invalid_participants", "participants", f"At most {MAX_PEOPLE} people."
        )
    contact_ids = [p.contact_id for p in body.participants if p.contact_id]
    if len(set(contact_ids)) != len(contact_ids):
        raise _unprocessable(
            "invalid_participants", "participants", "Someone is in it twice."
        )
    contacts = {
        c.id: c
        for c in await session.scalars(
            select(SplitContact).where(
                SplitContact.household_id == household.id,
                SplitContact.id.in_(contact_ids),
            )
        )
    }
    if len(contacts) != len(contact_ids):
        raise _unprocessable(
            "invalid_participants", "participants", "That person isn't saved."
        )

    people = len(body.participants)
    tax = _money(body.tax, currency, "tax", positive=False)
    tip = _money(body.tip, currency, "tip", positive=False)
    items: list[tuple[str, int, list[int]]] = []
    if body.mode == "equal":
        total = _money(body.total, currency, "total", positive=True)
        shares = service.equal_shares(total, people)
        tax = tip = 0
    else:
        if not body.items:
            raise _unprocessable("invalid_items", "items", "Add the bill's items.")
        for index, item in enumerate(body.items):
            amount = _money(
                item.amount, currency, f"items.{index}.amount", positive=True
            )
            who = sorted(set(item.participants))
            if not who or any(i < 0 or i >= people for i in who):
                raise _unprocessable(
                    "invalid_items", f"items.{index}.participants", "Say who had it."
                )
            items.append((item.name.strip(), amount, who))
        shares = service.item_shares(people, [(a, w) for _, a, w in items], tax, tip)
        total = sum(a for _, a, _ in items) + tax + tip

    split = Split(
        household_id=household.id,
        title=body.title.strip(),
        occurred_on=occurred_on,
        mode=body.mode,
        currency=currency,
        total_minor_units=total,
        tax_minor_units=tax,
        tip_minor_units=tip,
    )
    session.add(split)
    await session.flush()
    participants = []
    for position, (person, share) in enumerate(zip(body.participants, shares)):
        contact = contacts.get(person.contact_id) if person.contact_id else None
        participant = SplitParticipant(
            household_id=household.id,
            split_id=split.id,
            contact_id=contact.id if contact else None,
            is_self=contact is None,
            name=contact.name if contact else (user.display_name or ""),
            phone=contact.phone if contact else None,
            position=position,
            currency=currency,
            share_minor_units=share,
        )
        session.add(participant)
        participants.append(participant)
    await session.flush()
    for position, (name, amount, who) in enumerate(items):
        item = SplitItem(
            household_id=household.id,
            split_id=split.id,
            name=name,
            currency=currency,
            amount_minor_units=amount,
            position=position,
        )
        session.add(item)
        await session.flush()
        for i in who:
            session.add(
                SplitItemShare(
                    household_id=household.id,
                    item_id=item.id,
                    participant_id=participants[i].id,
                )
            )
    await session.commit()
    return _split_out(await _loaded(session, household.id, split.id))


@router.get("", response_model=SplitsOut)
async def list_splits(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> SplitsOut:
    """The household's splits, newest first."""
    rows = await session.scalars(
        select(Split)
        .where(Split.household_id == identity.household.id)
        .options(selectinload(Split.participants))
        .order_by(Split.created_at.desc(), Split.id)
        .limit(200)
    )
    out = []
    for split in rows:
        owed = sum(
            p.share_minor_units
            for p in split.participants
            if not p.is_self and p.settled_at is None
        )
        out.append(
            SplitSummaryOut(
                id=split.id,
                title=split.title,
                occurred_on=split.occurred_on.isoformat(),
                total=from_minor_units(split.total_minor_units, split.currency),
                people=len(split.participants),
                owed_to_you=from_minor_units(owed, split.currency),
                settled=owed == 0,
            )
        )
    return SplitsOut(splits=out)


@router.get("/{split_id}", response_model=SplitOut)
async def read_split(
    split_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> SplitOut:
    return _split_out(await _loaded(session, identity.household.id, split_id))


@router.patch("/{split_id}/participants/{participant_id}", response_model=SplitOut)
async def settle(
    split_id: uuid.UUID,
    participant_id: uuid.UUID,
    body: SettleIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> SplitOut:
    """Mark someone's share paid, or not paid after all."""
    household_id = identity.household.id
    split = await _loaded(session, household_id, split_id)
    participant = next((p for p in split.participants if p.id == participant_id), None)
    if participant is None or participant.is_self:
        raise _not_found()
    participant.settled_at = datetime.now(UTC) if body.settled else None
    await session.commit()
    return _split_out(await _loaded(session, household_id, split_id))


@router.delete("/{split_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_split(
    split_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> Response:
    split = await _loaded(session, identity.household.id, split_id)
    await session.delete(split)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
