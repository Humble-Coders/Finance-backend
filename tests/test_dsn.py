"""Tests for database URL normalization.

Every case here is a real string Supabase's dashboard can hand you.
"""

import pytest

from app.core.dsn import normalize_async_dsn

HOST = "aws-0-us-east-1.pooler.supabase.com:6543"


class TestScheme:
    @pytest.mark.parametrize("scheme", ["postgresql", "postgres", "postgresql+psycopg2"])
    def test_sync_schemes_become_asyncpg(self, scheme):
        result = normalize_async_dsn(f"{scheme}://u:p@{HOST}/postgres")
        assert result == f"postgresql+asyncpg://u:p@{HOST}/postgres"

    def test_already_async_is_untouched(self):
        url = f"postgresql+asyncpg://u:p@{HOST}/postgres"
        assert normalize_async_dsn(url) == url


class TestLibpqParams:
    @pytest.mark.parametrize(
        "param", ["sslmode=require", "channel_binding=require", "gssencmode=disable"]
    )
    def test_dropped(self, param):
        result = normalize_async_dsn(f"postgresql://u:p@{HOST}/postgres?{param}")
        assert result == f"postgresql+asyncpg://u:p@{HOST}/postgres"

    def test_other_params_survive(self):
        result = normalize_async_dsn(
            f"postgresql://u:p@{HOST}/postgres?sslmode=require&application_name=finai"
        )
        assert result == f"postgresql+asyncpg://u:p@{HOST}/postgres?application_name=finai"


class TestCredentials:
    def test_password_with_special_characters_survives(self):
        url = f"postgresql://postgres.abc:p%40ss%2Fword@{HOST}/postgres"
        assert normalize_async_dsn(url) == (
            f"postgresql+asyncpg://postgres.abc:p%40ss%2Fword@{HOST}/postgres"
        )

    def test_whitespace_is_trimmed(self):
        url = f"  postgresql://u:p@{HOST}/postgres  "
        assert normalize_async_dsn(url) == f"postgresql+asyncpg://u:p@{HOST}/postgres"


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_rejected(bad):
    with pytest.raises(ValueError, match="empty"):
        normalize_async_dsn(bad)
