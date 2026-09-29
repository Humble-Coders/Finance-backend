"""Filing saved transactions into categories.

Shared by every path that writes transactions — a confirmed import (3.3) and a
transaction typed in by hand (#38) — so the order of precedence is written
once: a category the person chose is never touched (callers only pass rows
without one); the household's own correction rules come next, applied exactly;
the model is asked last, and only with consent; anything still unfiled goes to
the review queue, never silently uncategorized.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.money import from_minor_units
from app.models.categorization import Category
from app.models.enums import ReviewReason
from app.models.money import Transaction
from app.services.categorization import categorize
from app.services.corrections import apply_rules
from app.services.llm import LlmError, build_client, close_client

__all__ = ["file_rows"]

log = structlog.get_logger()


async def file_rows(
    session: AsyncSession,
    settings: Settings,
    household_id: uuid.UUID,
    saved_ids: list[uuid.UUID],
    *,
    may_ask_model: bool,
) -> None:
    """Categorize what was just saved, on merchant and amount alone.

    Runs after the rows exist so a model outage cannot cost the import: the
    transactions are already written, and an uncategorized row simply goes to
    review, which is where it belongs anyway.

    `may_ask_model` is whether this person has agreed to AI processing. An
    import always has — the parse endpoint refuses without it. A transaction
    typed in by hand (#38) need not have: someone trying FinAI with three
    entries may never have been asked. Without it the household's own rules
    still apply, because they involve no model; anything they do not cover
    goes to a person rather than to a provider the person never agreed to
    (PRD Appendix A.5).
    """
    result = await session.execute(
        select(Transaction).where(Transaction.id.in_(saved_ids))
    )
    all_rows = list(result.scalars().all())

    # A row whose description held no name — all reference numbers, say — has
    # nothing to categorize *with*. Asking the model to file `["", "5.25"]`
    # buys an answer that looks confident and cannot be better than a guess.
    # It goes straight to a person instead, which is cheaper and honest.
    def send_to_review(rows) -> None:
        """A transaction with no category belongs in front of a person.

        Every path out of this function that leaves a row uncategorized has to
        call this. Rows are written before categorization runs precisely so a
        model problem costs nothing — but "costs nothing" means the row still
        reaches somebody, not that it lands silently with an empty category
        while the review queue says all is well. M4's budgets read categories.
        """
        for row in rows:
            row.needs_review = True
            row.review_reason = row.review_reason or ReviewReason.unknown_category

    # A row whose description held no name — all reference numbers, say — has
    # nothing to categorize *with*. Asking the model to file `["", "5.25"]`
    # buys an answer that looks confident and cannot be better than a guess.
    send_to_review([row for row in all_rows if not row.merchant])

    rows = [row for row in all_rows if row.merchant]
    if not rows:
        return

    # The household's own rules first, applied exactly. Before the client is
    # built, so a rule still holds when the model is unreachable; and the rows
    # it files are never sent to the model at all, which is both less to send
    # and the only way a rule into the household's *own* category can work —
    # the model may only answer with shared ones.
    ruled = {row.id for row in await apply_rules(session, household_id, rows)}
    rows = [row for row in rows if row.id not in ruled]
    if not rows:
        return

    if not may_ask_model:
        send_to_review(rows)
        return

    try:
        # Inside the try: `build_client` raises LlmError for a missing key,
        # model or provider. Outside it, that misconfiguration propagates, the
        # transaction rolls back, and the import the user just confirmed is
        # lost to a 500 — which is the opposite of why categorization runs
        # after the rows are written. `categorize` already degrades on its own
        # once it has a client; this is the same promise, one step earlier.
        client = build_client(settings)
    except LlmError:
        # The likely failure, not the exotic one: a misspelled LLM_PROVIDER is
        # a deployment mistake somebody makes once. Without this, a month of
        # imports would land with no categories and nothing in the review queue
        # saying so — and `categorize` flags this same condition when it fails
        # further in, so the two paths disagreed about the same event.
        log.warning("categorization_skipped", reason="client_unavailable")
        send_to_review(rows)
        return

    try:
        # The entire payload: a shop name and a price. Nothing else may be
        # added here (PRD Appendix A.3) — there is a test that asserts it.
        suggestions = await categorize(
            session,
            client,
            household_id=household_id,
            pairs=[
                (
                    row.merchant or "",
                    from_minor_units(row.amount_minor_units, row.currency),
                )
                for row in rows
            ],
        )
    finally:
        await close_client(client)

    slugs = await session.execute(
        select(Category.slug, Category.id).where(Category.household_id.is_(None))
    )
    by_slug = {slug: ident for slug, ident in slugs.all()}

    for row, suggestion in zip(rows, suggestions, strict=False):
        row.category_id = by_slug.get(suggestion.slug)
        if not suggestion.recognised:
            row.needs_review = True
            row.review_reason = row.review_reason or ReviewReason.unknown_category
