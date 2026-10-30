"""India's country pack, in rupees, with its disclaimer

Revision ID: a7c4e2f9b318
Revises: e5a1c8d4f702
Create Date: 2026-10-30

A household whose phone number is +91 already resolves to IN, but with no pack
behind it the API served the documented fallback — Canadian dollars and en-CA —
so an Indian tester saw every figure in $. This is the pack: INR, en-IN, the
tax-advantaged account types, and a regional disclaimer.

Launched, because the team tests the Indian experience end to end, content
included. The disclaimer is copy for that testing, not reviewed by counsel: it
is replaced before a public Indian launch by adding a new version, never by
editing this row, as consent and display are logged against the row shown.

Same namespace and id scheme as the CA pack (ff7d60d4b3b8) and its disclaimer
(5b2e9f7c1d34), so the ids are the same in every environment.
"""

import uuid

from alembic import op

revision = "a7c4e2f9b318"
down_revision = "e5a1c8d4f702"
branch_labels = None
depends_on = None

NAMESPACE = uuid.UUID("6f3d9f5e-2a1c-4f52-9d3b-1c7a5e0b4d21")
IN_COUNTRY_PACK_ID = str(uuid.uuid5(NAMESPACE, "country_pack:IN"))
IN_DISCLAIMER_ID = str(uuid.uuid5(NAMESPACE, "disclaimer_version:IN:in-v1"))

DISCLAIMER_BODY = (
    "FinAI is an educational tool for understanding your own money. It is not "
    "investment, tax or legal advice, and FinAI is not registered with SEBI as "
    "an investment adviser or research analyst. Budgets, projections and "
    "suggestions are estimates from the information you have given the app; "
    "they are not guarantees, and what actually happens will differ. "
    "Investments such as mutual funds, ELSS, NPS and shares are subject to "
    "market risk. Tax rules change: check the current rules, or speak to a "
    "qualified professional, before acting. Decisions about your money remain "
    "yours."
)


def upgrade() -> None:
    body = DISCLAIMER_BODY.replace("'", "''")
    op.execute(
        f"""
        INSERT INTO disclaimer_version
            (id, country_code, version, kind, body, effective_from, created_at, updated_at)
        VALUES
            ('{IN_DISCLAIMER_ID}', 'IN', 'in-v1', 'regional_disclaimer', '{body}', now(), now(), now())
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        f"""
        INSERT INTO country_pack
            (id, country_code, currency, locale, tax_accounts, disclaimer_version,
             is_launched, created_at, updated_at)
        VALUES
            ('{IN_COUNTRY_PACK_ID}', 'IN', 'INR', 'en-IN',
             '["PPF", "EPF", "NPS", "ELSS"]'::jsonb, 'in-v1', true, now(), now())
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM country_pack WHERE id = '{IN_COUNTRY_PACK_ID}'")
    op.execute(f"DELETE FROM disclaimer_version WHERE id = '{IN_DISCLAIMER_ID}'")
