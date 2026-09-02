"""seed system categories and ca country pack

Revision ID: ff7d60d4b3b8
Revises: f5f0f4fbadb4
Create Date: 2026-09-02 10:04:28.913845
"""
import uuid

from alembic import op
import sqlalchemy as sa


revision = 'ff7d60d4b3b8'
down_revision = 'f5f0f4fbadb4'
branch_labels = None
depends_on = None


# Deterministic ids, derived from a fixed namespace, so a given category has the
# same id in every environment. That matters the moment anything references a
# category id across environments — a seeded id that differs between staging and
# production is a debugging trap.
NAMESPACE = uuid.UUID("6f3d9f5e-2a1c-4f52-9d3b-1c7a5e0b4d21")


def _id(slug: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"category:{slug}"))


# The starting taxonomy. Users add their own on top; these are shared by every
# household, which is why category.household_id is nullable.
SYSTEM_CATEGORIES = [
    ("groceries", "Groceries"),
    ("dining", "Dining out"),
    ("rent", "Rent & mortgage"),
    ("utilities", "Utilities"),
    ("transport", "Transport"),
    ("subscriptions", "Subscriptions"),
    ("shopping", "Shopping"),
    ("healthcare", "Healthcare"),
    ("personal_care", "Personal care"),
    ("entertainment", "Entertainment"),
    ("travel", "Travel"),
    ("education", "Education"),
    ("insurance", "Insurance"),
    ("fees", "Fees & charges"),
    ("income", "Income"),
    ("transfers", "Transfers"),
    ("savings", "Savings"),
    ("debt_payment", "Debt payment"),
    ("gifts_donations", "Gifts & donations"),
    ("other", "Other"),
]

CA_COUNTRY_PACK_ID = str(uuid.uuid5(NAMESPACE, "country_pack:CA"))


def upgrade() -> None:
    """Seed the system taxonomy and the launch market's country pack.

    Both use ON CONFLICT DO NOTHING so re-running is safe — the queue migration
    is not the only one that has to tolerate a database that has seen it before.
    """
    for slug, name in SYSTEM_CATEGORIES:
        op.execute(
            f"""
            INSERT INTO category (id, household_id, slug, name, created_at, updated_at)
            VALUES ('{_id(slug)}', NULL, '{slug}', '{name.replace("'", "''")}', now(), now())
            ON CONFLICT DO NOTHING
            """
        )

    # Canada is the launch market (PRD §4.6). Adding another country is an
    # INSERT here — no code change, no deploy.
    op.execute(
        f"""
        INSERT INTO country_pack
            (id, country_code, currency, locale, tax_accounts, disclaimer_version,
             is_launched, created_at, updated_at)
        VALUES
            ('{CA_COUNTRY_PACK_ID}', 'CA', 'CAD', 'en-CA',
             '["RRSP", "TFSA", "FHSA"]'::jsonb, 'ca-v1', true, now(), now())
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM country_pack WHERE id = '{CA_COUNTRY_PACK_ID}'")
    slugs = ", ".join(f"'{_id(slug)}'" for slug, _ in SYSTEM_CATEGORIES)
    op.execute(f"DELETE FROM category WHERE id IN ({slugs})")
