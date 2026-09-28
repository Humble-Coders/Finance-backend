"""What a category correction teaches, and where it applies straight away.

A correction is not a fix to one row. Filing one Spotify charge under
Entertainment is the user telling us what Spotify *is* for their household, so
it becomes a rule: shown to the categorizer on every future import (PRD F3,
§4.5 — prompt-side, never training), and applied now to the rows still waiting
in the queue.

**Never across households.** Every query here carries `household_id` in its
`WHERE` clause rather than filtering afterwards. One person's labels are not
training data for anyone else, and that is a promise in Appendix A, not a
preference.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.categorization import CategoryCorrection
from app.models.money import Transaction

__all__ = ["Learned", "merchant_key", "learn"]


def merchant_key(merchant: str | None) -> str | None:
    """The form a merchant is matched on: lower case, single spaces.

    Normalisation, not fuzziness. "Cafe Luna" typed by one person and
    "cafe  luna" typed by the next are the same merchant, and without this they
    would be two rules — which the one-rule-per-merchant constraint exists to
    prevent, and which a prompt would then show as two answers for one name.

    Anything looser — "Spotify P3a4b5c6" matching "Spotify Q9r8s7t6" — is
    similarity rather than normalisation, and belongs to merchant search (6.2).

    `lower()` rather than `casefold()` on purpose: the database compares with
    Postgres's `lower()`, and the two differ on characters like `ß`. Matching
    the key the database computes is worth more than matching Unicode's ideal.
    """
    if not merchant:
        return None
    key = " ".join(merchant.split()).lower()
    return key or None


def _sql_merchant_key():
    """`merchant_key`, computed by the database, so the two always agree."""
    return func.lower(
        func.regexp_replace(func.trim(Transaction.merchant), r"\s+", " ", "g")
    )


@dataclass(frozen=True)
class Learned:
    """What one correction did."""

    # False when the row has no merchant to generalise from. The row itself is
    # still corrected; there is simply nothing to learn a rule about.
    rule_recorded: bool
    # Other rows in the queue that took the new category.
    recategorized: int


async def learn(
    session: AsyncSession,
    *,
    household_id: uuid.UUID,
    row: Transaction,
    predicted_category_id: uuid.UUID | None,
    corrected_category_id: uuid.UUID,
) -> Learned:
    """Record the rule this correction states, and apply it to the queue.

    Call it after the row itself has been updated, so the merchant it learns
    from is the one the user just confirmed rather than the one they corrected.
    """
    pattern = merchant_key(row.merchant)
    if pattern is None:
        return Learned(rule_recorded=False, recategorized=0)

    # An upsert on the unique key rather than read-then-write: two review
    # screens open on the same merchant would each read "no rule yet" and each
    # insert one, and the constraint would refuse the second as an error the
    # user did nothing to cause. Correcting again replaces the rule — the
    # latest answer is the one that holds.
    statement = pg_insert(CategoryCorrection).values(
        household_id=household_id,
        transaction_id=row.id,
        merchant_pattern=pattern,
        predicted_category_id=predicted_category_id,
        corrected_category_id=corrected_category_id,
        updated_at=func.clock_timestamp(),
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[
                CategoryCorrection.household_id,
                CategoryCorrection.merchant_pattern,
            ],
            set_={
                "corrected_category_id": statement.excluded.corrected_category_id,
                "predicted_category_id": statement.excluded.predicted_category_id,
                "transaction_id": statement.excluded.transaction_id,
                # Set by hand: `onupdate` is an ORM hook and an upsert does not
                # fire it. It matters here because the categorizer shows the
                # most recently updated rules first and keeps only a handful —
                # a rule the user has just re-corrected belongs at the front,
                # not wherever it was first written.
                #
                # `clock_timestamp()`, not `now()`: `now()` is the start of the
                # transaction, so every rule written in one transaction shares
                # it and their order is arbitrary. This is the moment of the
                # write, which is what "most recently corrected" means.
                "updated_at": func.clock_timestamp(),
            },
        )
    )

    # Applied to rows still waiting, and only those. A row the user already
    # confirmed was answered by them, about that row; a rule written later
    # about the merchant does not get to overrule them.
    #
    # The rows take the category and stay in the queue. The user has not looked
    # at them — a low-confidence row may still have the wrong amount — and
    # marking them reviewed would say otherwise.
    moved = await session.execute(
        update(Transaction)
        .where(
            Transaction.household_id == household_id,
            Transaction.needs_review.is_(True),
            Transaction.id != row.id,
            _sql_merchant_key() == pattern,
            Transaction.category_id.is_distinct_from(corrected_category_id),
        )
        .values(category_id=corrected_category_id)
        .execution_options(synchronize_session=False)
    )
    return Learned(rule_recorded=True, recategorized=moved.rowcount or 0)
