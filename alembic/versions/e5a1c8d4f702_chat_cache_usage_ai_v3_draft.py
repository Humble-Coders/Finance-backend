"""The AI coach's answer cache and daily usage, and ai-v3 as a draft (#71)

Revision ID: e5a1c8d4f702
Revises: d7b3f9a1c426
Create Date: 2026-10-29

`chat_answer_cache` keeps a model's answer for the same standalone question
over the same ledger (`ledger_version`, #83), so asking again costs nothing.
`chat_daily_usage` counts the questions a model answered per household per
day, for the daily cap — apart from the conversations, so deleting one does
not give questions back.

ai-v3 of the AI-processing policy adds what chat sends. Seeded undated, as
ai-v2 was: putting it in force is a product and legal step before launch.
"""

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e5a1c8d4f702"
down_revision = "d7b3f9a1c426"
branch_labels = None
depends_on = None

NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
POLICY_ID = str(uuid.uuid5(NAMESPACE, "disclaimer_version:ai-processing:ai-v3"))

# ai-v2's text, with what chat sends added after what typing a transaction
# sends. Every sentence true of the system as built (`app/services/chat.py`).
BODY = """\
To read your statement, FinAI needs to send its contents to an AI service.

What is sent: the text of the statement, after your phone has removed your \
name, address and account number. Dates, descriptions and amounts remain, \
because those are the transactions.

What is not sent: the statement file itself. It never leaves your phone.

When you type a transaction in yourself and leave its category for us to \
choose, its shop name and amount are sent to choose one. Nothing else about \
it is sent.

When you ask the AI coach a question, your question and your last few \
messages in that conversation are sent, with the figures from your own \
records needed to answer it: totals, and where a question needs them, \
transactions' dates, amounts, categories, shop names and descriptions. \
Suggested questions are answered by FinAI itself, and send nothing.

We only send it to providers working on our instructions under a contract that \
forbids training on your data and requires them not to keep it.

You can withdraw this consent at any time in Settings. After that nothing more \
is sent: you cannot import statements or ask the AI coach your own questions, \
and a transaction you type in is categorized only by your own earlier \
corrections, or left for you to choose. Withdrawing does not delete the \
transactions you have already saved. You can give consent again whenever you \
like.
"""


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "chat_answer_cache",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("household_id", sa.UUID(), nullable=False),
        sa.Column("question_hash", sa.String(length=64), nullable=False),
        sa.Column("ledger_version", sa.BigInteger(), nullable=False),
        sa.Column("answer", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_chat_answer_cache_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_answer_cache")),
    )
    op.create_index(
        op.f("ix_chat_answer_cache_household_id"),
        "chat_answer_cache",
        ["household_id"],
    )
    op.create_index(
        "uq_chat_answer_cache_key",
        "chat_answer_cache",
        ["household_id", "question_hash", "ledger_version"],
        unique=True,
    )

    op.create_table(
        "chat_daily_usage",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("household_id", sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("model_answers", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_chat_daily_usage_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_daily_usage")),
    )
    op.create_index(
        op.f("ix_chat_daily_usage_household_id"),
        "chat_daily_usage",
        ["household_id"],
    )
    op.create_index(
        "uq_chat_daily_usage_day",
        "chat_daily_usage",
        ["household_id", "day"],
        unique=True,
    )

    # Row level security on, as on every household-scoped table (CLAUDE.md).
    op.execute("ALTER TABLE chat_answer_cache ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE chat_daily_usage ENABLE ROW LEVEL SECURITY")

    op.execute(
        f"""
        INSERT INTO disclaimer_version
            (id, country_code, version, kind, body, effective_from,
             created_at, updated_at)
        VALUES ('{POLICY_ID}', NULL, 'ai-v3', 'ai_processing',
                '{BODY.replace("'", "''")}', NULL, now(), now())
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    # RESTRICT from consent_event and consent_change: refuses if anyone has
    # already agreed to ai-v3, which is right — their consent is evidence.
    op.execute(f"DELETE FROM disclaimer_version WHERE id = '{POLICY_ID}'")
    op.drop_index("uq_chat_daily_usage_day", table_name="chat_daily_usage")
    op.drop_index(
        op.f("ix_chat_daily_usage_household_id"), table_name="chat_daily_usage"
    )
    op.drop_table("chat_daily_usage")
    op.drop_index("uq_chat_answer_cache_key", table_name="chat_answer_cache")
    op.drop_index(
        op.f("ix_chat_answer_cache_household_id"), table_name="chat_answer_cache"
    )
    op.drop_table("chat_answer_cache")
