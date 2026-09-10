"""feature scope uniqueness

Revision ID: c3a91e7d4b28
Revises: b56e4dda349a
Create Date: 2026-09-10 09:12:04.117302
"""
from alembic import op
import sqlalchemy as sa


revision = 'c3a91e7d4b28'
down_revision = 'b56e4dda349a'
branch_labels = None
depends_on = None


# "Any country" and "any plan" are stored as NULL, and Postgres treats NULLs as
# distinct — so uq_feature_scope, indexing the bare columns, constrained only the
# one scope where both are set. The other three accepted duplicates:
#
#     global      (NULL, NULL)  accepted a second row
#     country only(CA,   NULL)  accepted a second row
#     plan only   (NULL, free)  accepted a second row
#     country+plan(CA,   free)  correctly rejected
#
# Two rows of equal specificity that disagree make resolution depend on physical
# row order, so a feature could flip on or off after a vacuum moved one. Same
# trap as the duplicate system-category slugs in 1daf2b084378, and the same fix:
# a partial index per scope, so the NULLs are in the predicate rather than in the
# indexed columns where they cannot be compared.
#
# One index per scope rather than a single COALESCE expression because casting
# the plan enum to text is only STABLE, not IMMUTABLE, so Postgres refuses it in
# an index expression.
NEW_INDEXES = [
    (
        "uq_feature_scope_global",
        ["feature_key"],
        "country_code IS NULL AND plan IS NULL",
    ),
    (
        "uq_feature_scope_country",
        ["feature_key", "country_code"],
        "country_code IS NOT NULL AND plan IS NULL",
    ),
    (
        "uq_feature_scope_plan",
        ["feature_key", "plan"],
        "country_code IS NULL AND plan IS NOT NULL",
    ),
]

# uq_feature_scope itself is left alone: it already covers the country+plan scope
# correctly, which is the one case where neither column is NULL.


def upgrade() -> None:
    # A duplicate already in the table would fail the CREATE with a bare
    # constraint error naming only the index. Surface the real problem instead:
    # which feature, which scope, how many rows.
    conn = op.get_bind()
    duplicates = conn.execute(
        sa.text(
            """
            SELECT feature_key,
                   COALESCE(country_code, '*') AS country,
                   COALESCE(plan::text, '*') AS plan,
                   count(*) AS n
            FROM feature_availability
            GROUP BY 1, 2, 3 HAVING count(*) > 1
            """
        )
    ).all()
    if duplicates:
        listed = "; ".join(
            f"{r.feature_key} ({r.country}/{r.plan}): {r.n} rows" for r in duplicates
        )
        raise RuntimeError(
            "feature_availability has rows of equal specificity that these "
            f"indexes would reject — resolve them by hand first: {listed}"
        )

    for name, columns, predicate in NEW_INDEXES:
        op.create_index(
            name,
            "feature_availability",
            columns,
            unique=True,
            postgresql_where=sa.text(predicate),
        )


def downgrade() -> None:
    for name, _, _ in reversed(NEW_INDEXES):
        op.drop_index(name, table_name="feature_availability")
