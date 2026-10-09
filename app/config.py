"""Application settings, loaded from environment variables.

Values never live in this repo — they come from the Render environment group
`finai-shared` in deployed environments, or a local .env file in development.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.dsn import normalize_async_dsn


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"

    # Supabase — use the TRANSACTION POOLER connection string (port 6543).
    # The direct :5432 connection will exhaust Postgres connections once the
    # API runs multiple instances.
    database_url: str

    # Migrations need a SESSION-mode connection (port 5432). DDL through the
    # transaction pooler (6543) is cancelled by its statement timeout — even a
    # trivial CREATE TABLE — because transaction mode is not built for it.
    # Falls back to database_url so a plain-Postgres environment (e.g. CI, which
    # has no pooler) needs no extra configuration.
    migration_database_url: str = ""

    supabase_url: str
    supabase_service_role_key: str = ""
    supabase_anon_key: str = ""
    # Legacy HS256 projects only. Prefer asymmetric keys + JWKS (see auth.py).
    supabase_jwt_secret: str = ""

    # The model provider is configuration on purpose. M3 starts on a free tier
    # against synthetic fixtures and must move to a paid, no-training tier
    # before any real statement is parsed (PRD Appendix A.3) — that swap has to
    # be an environment variable, or it will not happen on the day it must.
    llm_api_key: str = ""
    # The operator asserting that the configured key is a business API tier
    # whose terms forbid training on the data we send (PRD Appendix A.3). The
    # consent screen tells users this in so many words, so production refuses to
    # parse without it: a promise nobody can enforce is one we will eventually
    # break, and the free tier M3 develops against permits exactly what the
    # screen says is forbidden.
    #
    # **Before turning this on: put ai-v2 of the AI-processing policy in force**
    # (seeded undated by #42). Until this is on, production sends nothing to a
    # model at all; once it is, a transaction typed in without a category has
    # its shop name and amount sent (#38), which ai-v1 does not mention and
    # ai-v2 does. Consent to text that does not describe what happens is not
    # consent.
    llm_no_training_tier: bool = False
    llm_provider: str = "gemini"
    # gemini-2.0-flash, the default until October 2026, was shut down by Google
    # on 1 June 2026. Nothing broke only because every environment overrides
    # this; a deployment that forgot to would have failed every import.
    llm_model: str = "gemini-2.5-flash"
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"

    # Tokens a thinking model may spend reasoning per call, reserved on top of
    # the answer's own allowance so a long think cannot crowd the answer out.
    # Billed as output. 0 turns thinking off where the provider allows it.
    #
    # Off by default, on the evidence of the `llm_call` logs (2026-10-05).
    # With 2,048 every import after the change came back empty: a payment
    # receipt answered `[]` after ~600 tokens of thinking, and a screenshot's
    # seven rows were all rejected because the model copied `-$86.40` whole.
    # Without it the same texts parse fully, in a fifth of the tokens. Turn it
    # on for a model or a document that needs it, and read the logs after.
    llm_thinking_budget: int = 0

    # Free tier: one import a month (PRD F2). A setting rather than a constant
    # so development is not rationed by the production plan; 7.1 moves this into
    # entitlements, where per-plan limits belong.
    free_imports_per_month: int = 1

    # How many times a day a statement that was read but never saved may be
    # read again without counting as another import (#73). The app shows the
    # rows for checking before it saves them; if the phone kills the app
    # mid-check, it reads the file again, and that must not cost the month.
    # Bounded because every read is a model call somebody pays for.
    import_rereads_per_day: int = 3

    # Households the monthly import limit does not apply to: comma-separated
    # UUIDs, empty by default so the exemption exists only where it is
    # configured. For test accounts — importing the same statement twenty times
    # while working on the parser otherwise costs twenty months.
    #
    # An env var rather than a column, deliberately. There is one database and
    # it is production, so a schema change for a testing affordance is a
    # migration against real data; this is reversible by clearing a variable.
    # It also keeps the ids out of a public repository.
    #
    # It exempts from the COUNT, not from consent, ownership or any other
    # check — see app/api/statements.py. 7.1 folds this into entitlements with
    # the rest of the quota accounting.
    unlimited_import_households: str = ""

    @property
    def database_dsn(self) -> str:
        """`database_url` coerced into a form asyncpg accepts.

        Always use this, never `database_url` directly — the value pasted into
        Render is whatever Supabase's dashboard produced.
        """
        return normalize_async_dsn(self.database_url)

    @property
    def migration_dsn(self) -> str:
        """The DSN Alembic should use. Session-mode where one is configured."""
        return normalize_async_dsn(self.migration_database_url or self.database_url)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def unlimited_import_household_ids(self) -> frozenset[str]:
        """[unlimited_import_households] parsed, lowercased and de-blanked.

        Lowercased because a UUID pasted from the Supabase dashboard may be
        upper case while the one `str(household.id)` produces is not, and a
        quota exemption that silently does not apply is worse than none: the
        429 arrives looking like a bug in the limit rather than a typo in the
        configuration.
        """
        return frozenset(
            part.strip().lower()
            for part in self.unlimited_import_households.split(",")
            if part.strip()
        )

    @property
    def jwks_url(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
