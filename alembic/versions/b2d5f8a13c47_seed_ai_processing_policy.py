"""seed the AI-processing consent policy

Revision ID: b2d5f8a13c47
Revises: a1c4e7b90f22
Create Date: 2026-09-21

Separate from the migration that adds `policy_kind.ai_processing` on purpose:
Postgres refuses to use a new enum value in the same transaction that added it,
and Alembic runs each migration in a transaction. Merged into one file this
fails with "unsafe use of new value of enum type" — on the production run, not
in review.
"""

import uuid

from alembic import op

revision = "b2d5f8a13c47"
down_revision = "a1c4e7b90f22"
branch_labels = None
depends_on = None

NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
POLICY_ID = str(uuid.uuid5(NAMESPACE, "disclaimer_version:ai-processing:ai-v1"))

# Express, specific, and about one thing — bundling this with the account terms
# would make it not-express, which is the whole point of the requirement
# (PRD Appendix A.5 #1). Written to be read by a person, not a lawyer.
BODY = """\
To read your statement, FinAI needs to send its contents to an AI service.

What is sent: the text of the statement, after your phone has removed your \
name, address and account number. Dates, descriptions and amounts remain, \
because those are the transactions.

What is not sent: the statement file itself. It never leaves your phone.

The AI provider processes this on our instructions under a contract that \
forbids using it to train their models, and they do not keep it.

You can withdraw this consent at any time. Without it, you can still add \
transactions yourself.
"""


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO disclaimer_version
            (id, country_code, version, kind, body, effective_from,
             created_at, updated_at)
        VALUES ('{POLICY_ID}', NULL, 'ai-v1', 'ai_processing',
                '{BODY.replace("'", "''")}', now(), now(), now())
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM disclaimer_version WHERE id = '{POLICY_ID}'")
