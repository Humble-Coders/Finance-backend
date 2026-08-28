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

__all__ = ["normalize_async_dsn", "describe_dsn"]

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

    if scheme != _ASYNC_SCHEME:
        # Fail with something actionable. Without this the next symptom is
        # SQLAlchemy resolving some other dialect and dying on an unrelated
        # "No module named ..." several frames deep.
        raise ValueError(
            f"DATABASE_URL has unsupported scheme {parts.scheme!r}; "
            f"expected one of {sorted(_SYNC_SCHEMES)} or {_ASYNC_SCHEME!r}"
        )

    return urlunsplit((scheme, parts.netloc, parts.path, query, parts.fragment))


def describe_dsn(url: str) -> str:
    """Password-free description of a DSN, safe to log.

    The username IS included. It is not a secret — Supabase's pooler username is
    `postgres.<project-ref>`, and the project ref is already the subdomain of the
    public project URL. Showing it turns the most common pooler failure (a
    username missing its project ref, which Supavisor rejects as an unknown
    tenant) into something visible rather than something to guess at.

    >>> describe_dsn("postgresql+asyncpg://postgres.abc:secret@db.example.com:6543/postgres")
    'postgresql+asyncpg://postgres.abc@db.example.com:6543/postgres'
    """
    parts = urlsplit(url)
    host = parts.hostname or "?"
    port = f":{parts.port}" if parts.port else ""
    user = f"{parts.username}@" if parts.username else ""
    return f"{parts.scheme}://{user}{host}{port}{parts.path}"
