"""Categories a household makes for itself.

**No request can make a system category.** A system category has
`household_id` NULL and is seen by every household. This endpoint sets
`household_id` from the caller's token and reads nothing else that could
affect it — the request body may carry only a name, and anything more is
refused.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.db import get_session
from app.models.categorization import Category
from app.schemas.categories import CategoryIn, CategoryOut
from app.services.categories import slug_for
from app.services.conflicts import log_conflict
from app.services.identity import ResolvedIdentity

router = APIRouter(tags=["categories"])

CATEGORY_EXISTS = "category_exists"
HOUSEHOLD_SLUG_UNIQUE = "uq_category_household_slug"


@router.post(
    "/categories", response_model=CategoryOut, status_code=status.HTTP_201_CREATED
)
async def create_category(
    body: CategoryIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> CategoryOut:
    # Captured before any rollback below, which expires every loaded object.
    household_id = identity.household.id
    name = " ".join(body.name.split())
    slug = slug_for(name)
    if slug is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "unnamed_category",
                "field": "name",
                "message": "A category name needs at least one letter or number.",
            },
        )

    # A clash with a system category is refused too, even though the database
    # would allow it: its unique index covers a household's own slugs, and a
    # second "Groceries" beside the shared one would put two identical choices
    # in the picker with only one of them known to the categorizer. The answer
    # names the existing category, so the client can use it instead.
    existing = await _visible_by_slug(session, household_id, slug)
    if existing is not None:
        _refuse(household_id, "slug_already_visible_to_household", existing.id)

    category = Category(household_id=household_id, slug=slug, name=name)
    session.add(category)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # Two creates racing past the check above. The index is what holds.
        if HOUSEHOLD_SLUG_UNIQUE not in str(exc.orig):
            raise
        _refuse(household_id, "slug_created_concurrently", None)

    return CategoryOut(
        id=category.id,
        slug=category.slug,
        name=category.name,
        is_system=category.is_system,
    )


async def _visible_by_slug(
    session: AsyncSession, household_id: uuid.UUID, slug: str
) -> Category | None:
    """A category with this slug that the household can already see.

    System categories and its own — never another household's, which would
    turn this endpoint into a way to ask which names other people have used.
    """
    result = await session.execute(
        select(Category).where(
            Category.slug == slug,
            or_(
                Category.household_id.is_(None),
                Category.household_id == household_id,
            ),
        )
    )
    return result.scalars().first()


def _refuse(
    household_id: uuid.UUID, reason: str, existing_id: uuid.UUID | None
) -> None:
    log_conflict(CATEGORY_EXISTS, reason, household_id=str(household_id))
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": CATEGORY_EXISTS,
            "message": "You already have a category with that name.",
            "category_id": str(existing_id) if existing_id else None,
        },
    )
