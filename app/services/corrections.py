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

from sqlalchemy import func, select, update
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

    **This is the only definition.** Rows are matched against a rule by calling
    this on each candidate, not by a second version of it written in SQL. There
    used to be one, and it disagreed: Postgres `trim()` strips only spaces and
    its `\s` misses a non-breaking space, so `"\tCafe Luna"` and
    `"Cafe\u00a0Luna"` were one merchant here and two in the database. Two
    definitions of "the same merchant" is two chances to disagree about it.
    """
    if not merchant:
        return None
    key = " ".join(merchant.split()).lower()
    return key or None


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
    #
    # Candidates are read and matched with `merchant_key` itself, so the rule
    # and the rows it reaches use one definition of "the same merchant". A
    # household's queue is tens or hundreds of rows, so reading it is cheap.
    candidates = await session.execute(
        select(Transaction.id, Transaction.merchant).where(
            Transaction.household_id == household_id,
            Transaction.needs_review.is_(True),
            Transaction.id != row.id,
            Transaction.merchant.is_not(None),
            Transaction.category_id.is_distinct_from(corrected_category_id),
        )
    )
    matching = [
        ident
        for ident, merchant in candidates.all()
        if merchant_key(merchant) == pattern
    ]
    if not matching:
        return Learned(rule_recorded=True, recategorized=0)

    # `needs_review` and the household are checked again in the write itself:
    # a row confirmed by another request between the read and this update was
    # answered by the user, and must not have its category moved from under
    # that answer.
    moved = await session.execute(
        update(Transaction)
        .where(
            Transaction.id.in_(matching),
            Transaction.household_id == household_id,
            Transaction.needs_review.is_(True),
        )
        .values(category_id=corrected_category_id)
        .execution_options(synchronize_session=False)
    )
    return Learned(rule_recorded=True, recategorized=moved.rowcount or 0)
