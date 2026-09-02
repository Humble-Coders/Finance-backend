"""Constraint behaviour, proven against a real Postgres.

These cannot be unit tests: SQLite implements neither Postgres UUIDs, enums nor
partial unique indexes, so it would answer differently from production. Each
test runs in a rolled-back transaction (see conftest) and leaves no data.

Marked `integration`: backend CI (#13) runs without a database and skips them;
ticket #15 adds one and turns them on.
"""

from __future__ import annotations

import uuid

import pytest
from asyncpg.exceptions import UniqueViolationError

from tests.conftest import requires_db

pytestmark = [pytest.mark.integration, pytest.mark.asyncio, requires_db]


async def _household(db) -> uuid.UUID:
    hid = uuid.uuid4()
    await db.execute(
        "INSERT INTO household (id, country_code, created_at, updated_at)"
        " VALUES ($1, 'CA', now(), now())",
        hid,
    )
    return hid


async def _user(db, household_id, *, phone=None, auth_id=None) -> uuid.UUID:
    uid = uuid.uuid4()
    await db.execute(
        "INSERT INTO \"user\" (id, household_id, auth_user_id, phone, created_at, updated_at)"
        " VALUES ($1, $2, $3, $4, now(), now())",
        uid, household_id, auth_id or str(uuid.uuid4()), phone,
    )
    return uid


async def _account(db, household_id) -> uuid.UUID:
    aid = uuid.uuid4()
    await db.execute(
        "INSERT INTO account (id, household_id, name, kind, currency, created_at, updated_at)"
        " VALUES ($1, $2, 'Test', 'chequing', 'CAD', now(), now())",
        aid, household_id,
    )
    return aid


async def _transaction(db, household_id, account_id, *, description="STARBUCKS 4471"):
    await db.execute(
        """
        INSERT INTO transaction
            (id, household_id, account_id, occurred_on, amount_minor_units, currency,
             direction, normalized_description, source, needs_review, created_at, updated_at)
        VALUES ($1, $2, $3, DATE '2026-03-01', 575, 'CAD',
                'debit', $4, 'upload', false, now(), now())
        """,
        uuid.uuid4(), household_id, account_id, description,
    )


class TestTransactionDedup:
    """Re-importing an overlapping statement must not double-count."""

    async def test_identical_transaction_is_rejected(self, db):
        hid = await _household(db)
        aid = await _account(db, hid)
        await _transaction(db, hid, aid)

        with pytest.raises(UniqueViolationError):
            await _transaction(db, hid, aid)

    async def test_a_genuinely_different_transaction_is_allowed(self, db):
        """The constraint must not be so broad it blocks real duplicates.

        Two identical coffees on the same day at the same price are plausible —
        but they differ in description once normalized, which is what the key
        relies on.
        """
        hid = await _household(db)
        aid = await _account(db, hid)
        await _transaction(db, hid, aid, description="STARBUCKS 4471")
        await _transaction(db, hid, aid, description="TIM HORTONS 992")


class TestIdentity:
    """PRD §4.6 — the verified phone is what stops one person becoming two."""

    async def test_duplicate_phone_is_rejected(self, db):
        hid = await _household(db)
        await _user(db, hid, phone="+14165550101")

        other = await _household(db)
        with pytest.raises(UniqueViolationError):
            await _user(db, other, phone="+14165550101")

    async def test_multiple_users_may_have_no_phone(self, db):
        """NULLs are distinct, so unverified accounts do not collide."""
        hid = await _household(db)
        await _user(db, hid, phone=None)
        await _user(db, hid, phone=None)

    async def test_one_user_may_hold_several_providers(self, db):
        """Google, Apple and phone OTP must resolve to a single user."""
        hid = await _household(db)
        uid = await _user(db, hid, phone="+14165550102")

        for provider, external in (("google", "g-1"), ("apple", "a-1"), ("phone", "p-1")):
            await db.execute(
                "INSERT INTO user_identity (id, user_id, provider, provider_user_id,"
                " created_at, updated_at) VALUES ($1, $2, $3, $4, now(), now())",
                uuid.uuid4(), uid, provider, external,
            )

        count = await db.fetchval(
            "SELECT count(*) FROM user_identity WHERE user_id = $1", uid
        )
        assert count == 3

    async def test_the_same_provider_account_cannot_map_to_two_users(self, db):
        hid = await _household(db)
        first = await _user(db, hid)
        second = await _user(db, hid)

        await db.execute(
            "INSERT INTO user_identity (id, user_id, provider, provider_user_id,"
            " created_at, updated_at) VALUES ($1, $2, 'google', 'shared-id', now(), now())",
            uuid.uuid4(), first,
        )
        with pytest.raises(UniqueViolationError):
            await db.execute(
                "INSERT INTO user_identity (id, user_id, provider, provider_user_id,"
                " created_at, updated_at) VALUES ($1, $2, 'google', 'shared-id', now(), now())",
                uuid.uuid4(), second,
            )


class TestRegion:
    async def test_household_may_have_no_country_yet(self, db):
        """Google/Apple users authenticate before providing a phone."""
        hid = uuid.uuid4()
        await db.execute(
            "INSERT INTO household (id, country_code, created_at, updated_at)"
            " VALUES ($1, NULL, now(), now())",
            hid,
        )
        assert await db.fetchval(
            "SELECT country_code FROM household WHERE id = $1", hid
        ) is None


class TestMoney:
    async def test_negative_amounts_are_allowed(self, db):
        """Refunds, debts and overruns are legitimately negative."""
        hid = await _household(db)
        aid = await _account(db, hid)
        await db.execute(
            """
            INSERT INTO transaction
                (id, household_id, account_id, occurred_on, amount_minor_units, currency,
                 direction, normalized_description, source, needs_review, created_at, updated_at)
            VALUES ($1, $2, $3, DATE '2026-03-02', -2500, 'CAD',
                    'credit', 'REFUND', 'upload', false, now(), now())
            """,
            uuid.uuid4(), hid, aid,
        )

    async def test_currency_must_be_three_characters(self, db):
        hid = await _household(db)
        with pytest.raises(Exception):
            await db.execute(
                "INSERT INTO account (id, household_id, name, kind, currency,"
                " created_at, updated_at) VALUES ($1, $2, 'Bad', 'chequing', 'CA', now(), now())",
                uuid.uuid4(), hid,
            )
