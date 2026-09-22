"""Putting a transaction in a category, on the least data that can do it.

The PRD constrains this harder than anything else in the pipeline: the model
receives **`(merchant, amount)` pairs and nothing else** (F2 stage 5, Appendix
A.3). No dates, no account, no balances, no description tail. A list of shop
names and prices is close to anonymous, and that is the whole point — it is the
minimum payload that can still do the job, and it is demonstrable from this
file rather than promised in a policy.

The model never invents a category. It picks from the twenty slugs seeded in
M1, and an answer that is not one of them becomes `other`, flagged for review.
A slug is a stable identifier that budgets and the health score will key on for
years; a model is not allowed to mint one.

Corrections are per household. If someone tells us `CANADIAN TIRE` is shopping
rather than transport, next month's import knows — and no other household's
import does. That is Appendix A.5 #6, and it is why the query below filters on
`household_id` rather than being a nice-to-have index hint.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.categorization import Category, CategoryCorrection
from app.services.llm import LlmClient, LlmError

__all__ = ["Suggestion", "categorize", "OTHER", "MAX_EXAMPLES"]

log = structlog.get_logger()

OTHER = "other"
# Enough for the model to see a household's habits, few enough that one noisy
# correction cannot drown the taxonomy.
MAX_EXAMPLES = 20

_SYSTEM_PROMPT = """\
You put personal transactions into categories.

You are given a JSON array of [merchant, amount] pairs. Return ONLY a JSON array
of the same length, each element the category slug for the pair at that index.

Use ONLY these slugs:
{slugs}

Rules:
- Every element must be one of the slugs above. Never invent one.
- If you are unsure, use "other". A wrong confident answer costs more than
  "other", which a person can correct in seconds.
- A negative or incoming amount is usually "income" or "transfers".
{examples}"""


@dataclass(frozen=True)
class Suggestion:
    slug: str
    # False when the model returned something that is not a known slug. The row
    # becomes `other` and goes to review rather than being quietly filed.
    recognised: bool


async def _slugs(session: AsyncSession) -> dict[str, uuid.UUID]:
    """The system taxonomy, by slug. Seeded in M1; never added to from here."""
    result = await session.execute(
        select(Category.slug, Category.id).where(Category.household_id.is_(None))
    )
    return {slug: ident for slug, ident in result.all()}


async def _examples(session: AsyncSession, household_id: uuid.UUID) -> str:
    """This household's own corrections, as few-shot examples.

    Scoped by `household_id` in the query itself, not filtered afterwards: the
    promise is that one person's labels never reach another's prompt, and the
    safest place to keep that promise is the `WHERE` clause.
    """
    result = await session.execute(
        select(CategoryCorrection.merchant_pattern, Category.slug)
        .join(Category, Category.id == CategoryCorrection.corrected_category_id)
        .where(CategoryCorrection.household_id == household_id)
        .order_by(CategoryCorrection.updated_at.desc())
        .limit(MAX_EXAMPLES)
    )
    rows = result.all()
    if not rows:
        return ""
    lines = "\n".join(f'- "{pattern}" is {slug}' for pattern, slug in rows)
    return f"\nThis person has corrected these before. Follow them:\n{lines}"


async def categorize(
    session: AsyncSession,
    client: LlmClient,
    *,
    household_id: uuid.UUID,
    pairs: list[tuple[str, str]],
) -> list[Suggestion]:
    """One slug per pair, in order. Never raises on a bad answer — falls back.

    `pairs` is the entire payload: merchant strings and amounts. Anything else
    reaching the model would be a privacy regression, and a test asserts the
    request body carries nothing more.
    """
    if not pairs:
        return []

    known = await _slugs(session)
    prompt = _SYSTEM_PROMPT.format(
        slugs="\n".join(f"- {slug}" for slug in sorted(known)),
        examples=await _examples(session, household_id),
    )

    try:
        answer = await client.complete(
            system=prompt,
            user=json.dumps([[name, amount] for name, amount in pairs]),
            max_output_tokens=2_000,
        )
    except LlmError:
        # A model that will not answer must not lose the import. Everything
        # goes to review, which is exactly where an uncategorized row belongs.
        log.warning("categorization_unavailable", pairs=len(pairs))
        return [Suggestion(OTHER, recognised=False) for _ in pairs]

    slugs = _parse(answer, len(pairs))
    return [
        Suggestion(slug, recognised=True)
        if slug in known
        else Suggestion(OTHER, recognised=False)
        for slug in slugs
    ]


def _parse(answer: str, expected: int) -> list[str]:
    text = answer.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text).strip()
    try:
        payload = json.loads(text)
    except ValueError:
        return [OTHER] * expected
    if not isinstance(payload, list):
        return [OTHER] * expected
    # A short answer is padded and a long one truncated rather than raising:
    # the rows exist either way, and `other` sends them to a person.
    slugs = [str(item) if isinstance(item, str) else OTHER for item in payload]
    return (slugs + [OTHER] * expected)[:expected]
