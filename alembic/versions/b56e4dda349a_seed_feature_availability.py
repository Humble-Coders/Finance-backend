"""seed feature availability

Revision ID: b56e4dda349a
Revises: b046276164da
Create Date: 2026-09-10 00:06:29.592420
"""
import uuid

from alembic import op
import sqlalchemy as sa


revision = 'b56e4dda349a'
down_revision = 'b046276164da'
branch_labels = None
depends_on = None


# Deterministic ids so a row has the same identity in every environment.
NAMESPACE = uuid.UUID("1f6a2c94-7b30-4de6-9a1e-3c5d8b2f0a77")


def _id(key: str, country: str | None, plan: str | None) -> str:
    return str(uuid.uuid5(NAMESPACE, f"feature:{key}:{country}:{plan}"))


# Global defaults: every feature the clients know about, off until its milestone
# ships. Without these the payload would go silently empty when the hardcoded
# stub was removed — a client cannot distinguish "feature absent" from "feature
# off", so absence is not an acceptable way to say no.
#
# country_code and plan are NULL here, meaning "any" — the weakest specificity,
# so a per-country or per-plan row overrides them (see _specificity).
FEATURES = [
    ("bank_linking", "coming_soon"),
    ("tax_optimization", "coming_soon"),
    ("split_expenses", "coming_soon"),
    ("investment_tracking", "coming_soon"),
    ("document_upload", None),
    ("ai_chat", "coming_soon"),
]

# Enabled from the start: uploading is the core loop of v1.
ENABLED = {"document_upload"}


def upgrade() -> None:
    for key, reason in FEATURES:
        enabled = key in ENABLED
        reason_sql = "NULL" if enabled or reason is None else f"'{reason}'"
        op.execute(
            f"""
            INSERT INTO feature_availability
                (id, feature_key, country_code, plan, is_enabled, reason,
                 created_at, updated_at)
            VALUES ('{_id(key, None, None)}', '{key}', NULL, NULL,
                    {str(enabled).lower()}, {reason_sql}, now(), now())
            ON CONFLICT DO NOTHING
            """
        )


def downgrade() -> None:
    ids = ", ".join(f"'{_id(key, None, None)}'" for key, _ in FEATURES)
    op.execute(f"DELETE FROM feature_availability WHERE id IN ({ids})")
