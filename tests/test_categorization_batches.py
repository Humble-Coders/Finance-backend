"""Categorization in batches, so one import cannot outgrow one answer.

One call per import was the design, with a fixed 2,000-token answer — room for
about 350 slugs. A statement may hold 2,000 rows, and an answer cut off past
that point is not JSON, so every row of a large import fell back to `other` and
went to review. These pin the batches, and that a failure costs only its own.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.services.categorization import BATCH_SIZE, OTHER, categorize
from app.services.llm import LlmError
from tests.conftest import requires_db

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]


class Model:
    """Answers each batch with one slug per pair, and remembers what it saw."""

    model = "fake"

    def __init__(self, *, slug: str = "groceries", fail_on: set[int] | None = None):
        self.slug = slug
        self.fail_on = fail_on or set()
        self.calls: list[tuple[int, int]] = []  # (pairs, max_output_tokens)

    async def complete(self, *, system: str, user: str, max_output_tokens: int) -> str:
        pairs = json.loads(user)
        self.calls.append((len(pairs), max_output_tokens))
        if len(self.calls) - 1 in self.fail_on:
            raise LlmError("the answer was cut off at the token limit")
        return json.dumps([self.slug] * len(pairs))


def pairs(n: int) -> list[tuple[str, str]]:
    return [(f"SHOP {i}", "10.00") for i in range(n)]


async def test_a_large_import_is_asked_for_in_batches(db_session):
    model = Model()

    found = await categorize(
        db_session, model, household_id=uuid.uuid4(), pairs=pairs(250)
    )

    assert [n for n, _ in model.calls] == [BATCH_SIZE, BATCH_SIZE, 50]
    assert len(found) == 250
    assert all(s.slug == "groceries" and s.recognised for s in found)


async def test_each_answer_has_room_for_its_whole_batch(db_session):
    # A hundred slugs need well over the old fixed 2,000 tokens' share per
    # row; the room is sized to the batch, with a floor for a single row.
    model = Model()

    await categorize(db_session, model, household_id=uuid.uuid4(), pairs=pairs(101))

    full, single = model.calls
    assert full[1] >= BATCH_SIZE * 10
    assert single[1] >= 256


async def test_a_failed_batch_costs_only_its_own_rows(db_session):
    model = Model(fail_on={1})

    found = await categorize(
        db_session, model, household_id=uuid.uuid4(), pairs=pairs(250)
    )

    first, second, third = found[:100], found[100:200], found[200:]
    assert all(s.recognised for s in first), "the batch before is untouched"
    assert all(s.slug == OTHER and not s.recognised for s in second)
    assert all(s.recognised for s in third), "and so is the batch after"


async def test_answers_stay_in_the_order_the_rows_were_given(db_session):
    class Echo(Model):
        async def complete(self, *, system, user, max_output_tokens):
            got = json.loads(user)
            self.calls.append((len(got), max_output_tokens))
            # Odd shops are groceries, even ones dining: order is checkable.
            return json.dumps(
                [
                    "groceries" if int(name.split()[1]) % 2 else "dining"
                    for name, _ in got
                ]
            )

    found = await categorize(
        db_session, Echo(), household_id=uuid.uuid4(), pairs=pairs(230)
    )

    assert [s.slug for s in found] == [
        "groceries" if i % 2 else "dining" for i in range(230)
    ]


async def test_a_small_import_is_still_one_call(db_session):
    model = Model()

    await categorize(db_session, model, household_id=uuid.uuid4(), pairs=pairs(12))

    assert len(model.calls) == 1


async def test_nothing_to_categorize_asks_nothing(db_session):
    model = Model()

    assert (
        await categorize(db_session, model, household_id=uuid.uuid4(), pairs=[]) == []
    )
    assert model.calls == []
