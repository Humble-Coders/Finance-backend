"""Schema rules, enforced by scanning the metadata.

These assert the PRD's locked constraints against every table at once, so a new
model cannot quietly opt out of them. They need no database and run in CI.
"""

from __future__ import annotations

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.dialects.postgresql import UUID as PgUUID

import app.models  # noqa: F401 — registers every model with the metadata
from app.db import Base

METADATA = Base.metadata

# Two genuinely different cases, kept apart so neither hides the other.

# Global configuration and reference data — no household column at all.
NO_HOUSEHOLD = {
    "household",
    "country_pack",
    "feature_availability",
    "disclaimer_version",
}

# Reached through a parent, or scoped by a nullable column: these DO carry
# household_id (except where noted) but not via HouseholdScopedMixin.
#   user_identity -> user      budget_line -> budget
#   category       -> nullable, because system rows are shared by everyone
HOUSEHOLD_VIA_PARENT = {"user_identity", "budget_line"}
HOUSEHOLD_NULLABLE = {"category"}


def table_names() -> list[str]:
    return sorted(METADATA.tables)


class TestPrimaryKeys:
    """PRD §4.2 — UUID keys, no sequences; a second region would collide."""

    @pytest.mark.parametrize("name", table_names())
    def test_primary_key_is_a_single_uuid_column(self, name):
        table = METADATA.tables[name]
        pk = list(table.primary_key.columns)
        assert len(pk) == 1, f"{name} should have a single-column primary key"
        assert isinstance(pk[0].type, PgUUID), f"{name}.{pk[0].name} is not a UUID"

    @pytest.mark.parametrize("name", table_names())
    def test_no_autoincrement_sequences(self, name):
        for column in METADATA.tables[name].columns:
            assert not column.autoincrement is True, (
                f"{name}.{column.name} autoincrements; sequences collide across regions"
            )


def household_scoped_tables() -> list[str]:
    """Every table that carries household_id, including the nullable case.

    Previously `user`, `subscription_entitlement` and `category` sat in a single
    exclusion set alongside genuinely unscoped tables — so the assertion below
    silently skipped three tables that do have the column, while reading as
    though it covered everything.
    """
    return [
        n
        for n in table_names()
        if n not in NO_HOUSEHOLD and n not in HOUSEHOLD_VIA_PARENT
    ]


class TestHouseholdScoping:
    """PRD §4.4 — financial records belong to a household, not a user."""

    @pytest.mark.parametrize("name", household_scoped_tables())
    def test_has_household_id_with_fk_and_index(self, name):
        table = METADATA.tables[name]
        assert "household_id" in table.c, f"{name} is missing household_id"

        column = table.c.household_id
        assert column.foreign_keys, f"{name}.household_id has no foreign key"

        indexed = column.index or any(
            column.name in [c.name for c in idx.columns] for idx in table.indexes
        )
        assert indexed, f"{name}.household_id is not indexed"

    @pytest.mark.parametrize(
        "name", [n for n in household_scoped_tables() if n not in HOUSEHOLD_NULLABLE]
    )
    def test_household_id_is_not_nullable(self, name):
        """Only `category` may leave it NULL, for the shared system taxonomy."""
        assert METADATA.tables[name].c.household_id.nullable is False

    def test_the_exclusion_lists_are_honest(self):
        """A table listed as unscoped must genuinely have no household column.

        Otherwise an exclusion quietly turns into an exemption.
        """
        wrong = [n for n in NO_HOUSEHOLD if "household_id" in METADATA.tables[n].c]
        assert wrong == [], f"listed as unscoped but have household_id: {wrong}"


class TestMoneyColumns:
    """PRD §4.4 — integer minor units, always paired with a currency."""

    def test_every_amount_has_a_currency_sibling(self):
        missing = [
            f"{table.name}.{column.name}"
            for table in METADATA.tables.values()
            for column in table.columns
            if column.name.endswith("_minor_units") and "currency" not in table.c
        ]
        assert missing == [], f"money columns without a currency column: {missing}"

    def test_every_amount_is_bigint(self):
        wrong = [
            f"{table.name}.{column.name} is {column.type}"
            for table in METADATA.tables.values()
            for column in table.columns
            if column.name.endswith("_minor_units")
            and not isinstance(column.type, BigInteger)
        ]
        assert wrong == [], f"money columns must be BIGINT: {wrong}"

    def test_no_floating_point_columns_anywhere(self):
        """Floats and money do not mix — 0.1 + 0.2 != 0.3."""
        floats = [
            f"{table.name}.{column.name} is {column.type}"
            for table in METADATA.tables.values()
            for column in table.columns
            if column.type.__class__.__name__.upper()
            in {"FLOAT", "REAL", "DOUBLE", "DOUBLE_PRECISION", "NUMERIC", "DECIMAL"}
        ]
        assert floats == [], f"floating-point columns found: {floats}"

    def test_every_currency_column_has_a_length_check(self):
        """VARCHAR(3) caps length but accepts "CA" where "CAD" belongs.

        A CheckConstraint passed to mapped_column is silently discarded, so this
        asserts the constraint actually reached the metadata rather than trusting
        that it was declared.
        """
        missing = []
        for table in METADATA.tables.values():
            if "currency" not in table.c:
                continue
            checks = [
                c for c in table.constraints
                if c.__class__.__name__ == "CheckConstraint"
                and "currency" in str(getattr(c, "sqltext", ""))
            ]
            if not checks:
                missing.append(table.name)
        assert missing == [], f"currency columns without a length check: {missing}"

    def test_amounts_are_signed(self):
        """Refunds, debts and budget overruns are legitimately negative."""
        for table in METADATA.tables.values():
            for column in table.columns:
                if column.name.endswith("_minor_units"):
                    for constraint in table.constraints:
                        text = str(getattr(constraint, "sqltext", ""))
                        assert f"{column.name} >= 0" not in text, (
                            f"{table.name}.{column.name} forbids negatives"
                        )


class TestIdentityConstraints:
    """PRD §4.6 — the phone number is what stops one person becoming two."""

    def test_user_phone_is_unique(self):
        assert Base.metadata.tables["user"].c.phone.unique is True

    def test_user_phone_is_nullable(self):
        """Google/Apple users authenticate before providing one."""
        assert Base.metadata.tables["user"].c.phone.nullable is True

    def test_provider_identity_is_unique(self):
        table = METADATA.tables["user_identity"]
        pairs = {
            tuple(sorted(c.name for c in constraint.columns))
            for constraint in table.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        }
        assert ("provider", "provider_user_id") in pairs


class TestRegion:
    def test_household_country_code_is_nullable(self):
        """PRD §4.6 — unknown until phone verification. Never guessed."""
        assert METADATA.tables["household"].c.country_code.nullable is True


class TestTransactionDedup:
    def test_dedup_index_exists_and_is_unique(self):
        table = METADATA.tables["transaction"]
        dedup = {i.name: i for i in table.indexes}.get("uq_transaction_dedup")
        assert dedup is not None, "transaction dedup index is missing"
        assert dedup.unique is True
        assert [c.name for c in dedup.columns] == [
            "account_id",
            "occurred_on",
            "amount_minor_units",
            "normalized_description",
        ]

    def test_source_is_agnostic(self):
        """Aggregator support must need no schema change (PRD §4.4)."""
        table = METADATA.tables["transaction"]
        assert "source" in table.c
        assert "external_id" in table.c
        assert table.c.external_id.nullable is True


class TestNamingConvention:
    def test_metadata_has_a_naming_convention(self):
        """Unnamed constraints get unstable names, and downgrade() then breaks."""
        assert METADATA.naming_convention.get("uq")
        assert METADATA.naming_convention.get("fk")
