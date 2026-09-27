"""Financial accounts — the chequing account, the Visa, the savings account.

The minimum an import needs, not account management: create and list. Renaming,
archiving and balances are M4's problem.

**Why the name is unique per household.** The dedup key starts with
`account_id`, so two accounts for the same real account split a person's
statements into two piles that cannot see each other's duplicates. Re-import
January under "RBC" having filed it under "RBC Chequing" and every row lands
twice, with no error. A duplicate account is a silent double-counting bug
wearing a harmless-looking hat.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.db import get_session
from app.models.money import Account
from app.schemas.accounts import AccountIn, AccountOut
from app.services.capabilities import currency_for
from app.services.conflicts import log_conflict
from app.services.identity import ResolvedIdentity

router = APIRouter(tags=["accounts"])

DUPLICATE_ACCOUNT = "duplicate_account_name"
ACCOUNT_NAME_UNIQUE = "uq_account_household_name"


@router.get("/accounts", response_model=list[AccountOut])
async def list_accounts(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> list[Account]:
    """This household's accounts, oldest first, so the list is stable."""
    result = await session.execute(
        select(Account)
        .where(Account.household_id == identity.household.id)
        .order_by(Account.created_at)
    )
    return list(result.scalars().all())


@router.post(
    "/accounts", response_model=AccountOut, status_code=status.HTTP_201_CREATED
)
async def create_account(
    body: AccountIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> Account:
    household = identity.household
    # Captured before any rollback below: a rollback expires every ORM object
    # in the session, so reading `household.id` afterwards needs a database
    # round-trip — from a sync context, which raises MissingGreenlet rather
    # than the 409 we meant to send.
    household_id = household.id
    account = Account(
        household_id=household.id,
        name=body.name.strip(),
        kind=body.kind,
        currency=body.currency or await currency_for(session, household),
        institution=body.institution,
    )
    session.add(account)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # Check-then-act would race two simultaneous creates past each other;
        # the constraint is what actually holds, so it is what we answer from.
        if ACCOUNT_NAME_UNIQUE not in str(exc.orig):
            raise
        log_conflict(
            DUPLICATE_ACCOUNT,
            "name_already_used_in_household",
            household_id=str(household_id),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": DUPLICATE_ACCOUNT,
                "message": "You already have an account with that name.",
            },
        ) from exc

    return account
