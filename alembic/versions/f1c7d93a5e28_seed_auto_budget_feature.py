"""Seed the auto_budget feature, enabled for everyone

Revision ID: f1c7d93a5e28
Revises: e4b8a2c61f93
Create Date: 2026-10-06

A global row, so restricting it by plan later (7.1) is a more specific row —
a data change, not a deploy. Same namespace and id scheme as
b56e4dda349a_seed_feature_availability, so the row has one identity in every
environment.
"""

import uuid

from alembic import op

revision = "f1c7d93a5e28"
down_revision = "e4b8a2c61f93"
branch_labels = None
depends_on = None

NAMESPACE = uuid.UUID("1f6a2c94-7b30-4de6-9a1e-3c5d8b2f0a77")
KEY = "auto_budget"
ROW_ID = str(uuid.uuid5(NAMESPACE, f"feature:{KEY}:None:None"))


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO feature_availability
            (id, feature_key, country_code, plan, is_enabled, reason,
             created_at, updated_at)
        VALUES ('{ROW_ID}', '{KEY}', NULL, NULL, true, NULL, now(), now())
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM feature_availability WHERE id = '{ROW_ID}'")
