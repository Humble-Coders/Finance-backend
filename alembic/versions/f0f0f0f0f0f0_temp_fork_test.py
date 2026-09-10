"""TEMP: a second head, proving CI catches a forked migration chain (#15).

Shares c3a91e7d4b28 as its parent with d47f2b91c6ae. Removed in the next commit.
"""

revision = "f0f0f0f0f0f0"
down_revision = "c3a91e7d4b28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
