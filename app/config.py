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
    # API and worker each run multiple instances.
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

    llm_api_key: str = ""
    document_ai_credentials: str = ""

    extraction_queue_name: str = "extraction_jobs"

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
    def jwks_url(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
