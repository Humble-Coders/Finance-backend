"""Rebuild every household's monthly totals from its transactions (#83).

    python -m app.scripts.rebuild_rollups

Run once in production after the migration that creates
`monthly_category_total`, so households that imported before it have totals;
safe to run again at any time — each household is deleted and rebuilt from
its own rows, in its own transaction, so a failure part way leaves every
household either as it was or rebuilt, never half done.

Prints counts only. Which households, and what they spent, is theirs.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from app.db import get_engine, get_sessionmaker
from app.models.derived import MonthlyCategoryTotal
from app.models.identity import Household
from app.services import rollups


async def rebuild_all() -> tuple[int, int]:
    """Rebuild every household; returns (households, total rows written)."""
    sessions = get_sessionmaker()
    async with sessions() as session:
        ids = list((await session.execute(select(Household.id))).scalars())

    for household_id in ids:
        async with sessions() as session:
            await rollups.rebuild(session, household_id)
            await session.commit()

    async with sessions() as session:
        written = await session.scalar(select(func.count(MonthlyCategoryTotal.id)))
    return len(ids), int(written or 0)


async def _main() -> None:
    try:
        households, rows = await rebuild_all()
    finally:
        await get_engine().dispose()
    print(f"rebuilt monthly totals: {households} households, {rows} rows")


if __name__ == "__main__":
    asyncio.run(_main())
