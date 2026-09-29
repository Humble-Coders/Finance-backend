"""seed ai-v2 of the AI-processing policy, as a draft (#42)

Revision ID: 4f4b1595e986
Revises: a238d28cd057
Create Date: 2026-09-29

A new version rather than an edit: people have agreed to ai-v1's exact words,
and `disclaimer_version` is never changed once someone may have (its docstring,
and Appendix A.5 §1 — consent is provable only against the text as it was).

Two things ai-v1 does not say, and ai-v2 must, because every sentence of this
policy has to be true of the system as built (Appendix A.2):

* **Withdrawal.** ai-v1's draft promised it before it existed and the sentence
  was removed (b2d5f8a13c47). #42 builds it; this puts the sentence back.
* **Typed-in transactions.** Since #38, a transaction a person types in without
  a category has its shop name and amount sent to choose one. ai-v1 speaks
  only of statements.

**Seeded as a draft — `effective_from` is NULL** (manager decision,
2026-09-29). `current_policy` ignores undated rows, so nothing changes for
anyone yet: ai-v1 stays in force and nobody is asked to consent again. It says
"in Settings", and the phone has no such row until the mobile ticket ships;
dating ai-v2 then is a one-line migration, and at that moment everyone who
agreed to ai-v1 is asked again, which is what a changed policy requires.

It must be in force **before** `LLM_NO_TRAINING_TIER` is turned on in
production: until then production sends nothing to a model at all, which is
what keeps ai-v1's silence about typed-in entries true.
"""

import uuid

from alembic import op

revision = "4f4b1595e986"
down_revision = "a238d28cd057"
branch_labels = None
depends_on = None

NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
POLICY_ID = str(uuid.uuid5(NAMESPACE, "disclaimer_version:ai-processing:ai-v2"))

# ai-v1's text, then the two additions. Written to be read by a person, not a
# lawyer — and every sentence true of the system as built. That is why it does
# not say how to remove saved transactions: account deletion (Appendix A.5 §3)
# is not built, and pointing at it would be the false claim b2d5f8a13c47 had to
# remove. Say it in the version that goes live after it exists.
BODY = """\
To read your statement, FinAI needs to send its contents to an AI service.

What is sent: the text of the statement, after your phone has removed your \
name, address and account number. Dates, descriptions and amounts remain, \
because those are the transactions.

What is not sent: the statement file itself. It never leaves your phone.

When you type a transaction in yourself and leave its category for us to \
choose, its shop name and amount are sent to choose one. Nothing else about \
it is sent.

We only send it to providers working on our instructions under a contract that \
forbids training on your data and requires them not to keep it.

You can withdraw this consent at any time in Settings. After that nothing more \
is sent: you cannot import statements, and a transaction you type in is \
categorized only by your own earlier corrections, or left for you to choose. \
Withdrawing does not delete the transactions you have already saved. You can \
give consent again whenever you like.
"""


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO disclaimer_version
            (id, country_code, version, kind, body, effective_from,
             created_at, updated_at)
        VALUES ('{POLICY_ID}', NULL, 'ai-v2', 'ai_processing',
                '{BODY.replace("'", "''")}', NULL, now(), now())
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    # RESTRICT from consent_event and consent_change: this refuses if anyone has
    # already agreed to ai-v2, which is right — their consent is evidence.
    op.execute(f"DELETE FROM disclaimer_version WHERE id = '{POLICY_ID}'")
