"""ai-v2 draft: say typed-in entries *may* be sent, not that they are (#42)

Revision ID: 85ad4e1e92a0
Revises: 4f4b1595e986
Create Date: 2026-09-29

From the manager review of #42. ai-v2 said a typed-in transaction's shop name
and amount "are sent". They are not always: the household's own correction
rules file an entry first, and only what they leave uncovered reaches a model
(`app/services/filing.py`). Overstating what is sent is the safe direction
for a consent text, but every sentence is meant to be exact.

**Why a new migration and not an edit to 4f4b1595e986:** that one has already
run on production, so editing it would change nothing there and leave fresh
databases and production disagreeing about the text.

**Why editing this row is allowed at all:** `disclaimer_version` is never
changed once someone *may* have agreed to it. Nobody can have agreed to ai-v2:
it is undated, and `current_policy` never offers an undated row. The UPDATE
still checks both conditions — undated, and referenced by no consent — so it
does nothing, rather than rewrite agreed text, if either has changed.
"""

from alembic import op

revision = "85ad4e1e92a0"
down_revision = "4f4b1595e986"
branch_labels = None
depends_on = None

POLICY_ID = "84005b69-425d-5d4a-bc6d-f12fe6bd352e"

BEFORE = (
    "When you type a transaction in yourself and leave its category for us to "
    "choose, its shop name and amount are sent to choose one. Nothing else about "
    "it is sent."
)
AFTER = (
    "When you type a transaction in yourself and leave its category for us to "
    "choose, its shop name and amount may be sent to choose one, unless your own "
    "earlier corrections already cover it. Nothing else about it is sent."
)


def _swap(old: str, new: str) -> None:
    op.execute(
        f"""
        UPDATE disclaimer_version
        SET body = replace(body, '{old}', '{new}'), updated_at = now()
        WHERE id = '{POLICY_ID}'
          AND effective_from IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM consent_event
              WHERE disclaimer_version_id = '{POLICY_ID}')
          AND NOT EXISTS (
              SELECT 1 FROM consent_change
              WHERE disclaimer_version_id = '{POLICY_ID}')
        """
    )


def upgrade() -> None:
    _swap(BEFORE, AFTER)


def downgrade() -> None:
    _swap(AFTER, BEFORE)
