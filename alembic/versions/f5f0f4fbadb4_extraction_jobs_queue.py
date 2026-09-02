"""extraction jobs queue

Revision ID: f5f0f4fbadb4
Revises: 7ce291039fe7
Create Date: 2026-09-02 10:04:28.745386
"""
from alembic import op
import sqlalchemy as sa


revision = 'f5f0f4fbadb4'
down_revision = '7ce291039fe7'
branch_labels = None
depends_on = None


QUEUE_NAME = "extraction_jobs"


def upgrade() -> None:
    """Create the extraction queue, guarded twice.

    The queue already exists in production — it was created by hand in the SQL
    editor before this migration existed — so creating it unconditionally would
    fail against the very database this has to run on.

    It is also guarded on the pgmq extension being present, so a plain Postgres
    without pgmq (CI, ticket #15) runs this as a no-op rather than erroring.
    """
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgmq') THEN
                IF NOT EXISTS (
                    SELECT 1 FROM pgmq.list_queues() WHERE queue_name = '{QUEUE_NAME}'
                ) THEN
                    PERFORM pgmq.create('{QUEUE_NAME}');
                END IF;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    """Deliberately a no-op.

    Dropping the queue would destroy any undelivered extraction jobs — real
    user uploads mid-flight. A downgrade is run to undo a schema change, not to
    discard work, and leaving the queue in place is harmless: the upgrade is
    guarded, so re-applying finds it and does nothing.
    """
