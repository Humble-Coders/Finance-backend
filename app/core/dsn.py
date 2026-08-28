"""Database URL normalization.

Supabase's dashboard hands out libpq-style connection strings:

    postgresql://user:pw@host:6543/postgres?sslmode=require

Two things are wrong with that for us, and both fail at import time with errors
that don't name the real cause:

  * no driver in the scheme -> SQLAlchemy picks psycopg2, which we don't install
    ("ModuleNotFoundError: No module named 'psycopg2'")
  * `sslmode` and friends are libpq-only -> asyncpg rejects them as unexpected
    keyword arguments

Rather than relying on whoever pastes the string into Render getting it right,
we normalize here. asyncpg negotiates TLS with Supabase on its own, so dropping
`sslmode` does not make the connection unencrypted.
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = ["normalize_async_dsn"]

_ASYNC_SCHEME = "postgresql+asyncpg"

_SYNC_SCHEMES = {"postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"}

# libpq connection parameters asyncpg does not accept.
_LIBPQ_ONLY_PARAMS = {
    "sslmode",
    "channel_binding",
    "gssencmode",
    "target_session_attrs",
}


def normalize_async_dsn(url: str) -> str:
    """Coerce a Postgres URL into one asyncpg can use.

    >>> normalize_async_dsn("postgresql://u:p@h:6543/postgres?sslmode=require")
    'postgresql+asyncpg://u:p@h:6543/postgres'
    """
    if not url or not url.strip():
        raise ValueError("DATABASE_URL is empty")

    parts = urlsplit(url.strip())

    scheme = _ASYNC_SCHEME if parts.scheme in _SYNC_SCHEMES else parts.scheme

    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in _LIBPQ_ONLY_PARAMS
        ]
    )

    return urlunsplit((scheme, parts.netloc, parts.path, query, parts.fragment))
