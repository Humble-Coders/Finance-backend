"""Supabase JWT verification.

Clients authenticate against Supabase Auth; this API only *verifies* the token
it is handed (PRD §4.2 — "No custom auth is built"). Verification uses the
project's JWKS endpoint so key rotation needs no redeploy.

Legacy Supabase projects that still issue HS256 tokens signed with the shared
JWT secret fall back to `SUPABASE_JWT_SECRET`.
"""

from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings

_bearer = HTTPBearer(auto_error=True)
_jwks_client: jwt.PyJWKClient | None = None


@dataclass(frozen=True)
class AuthenticatedUser:
    """The verified caller. `user_id` is the Supabase auth uid."""

    user_id: str
    email: str | None
    phone: str | None
    claims: dict


def _get_jwks_client(settings: Settings) -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(settings.jwks_url, cache_keys=True)
    return _jwks_client


def _decode(token: str, settings: Settings) -> dict:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise _unauthorized("malformed token") from exc

    algorithm = header.get("alg", "")

    if algorithm.startswith(("RS", "ES")):
        signing_key = _get_jwks_client(settings).get_signing_key_from_jwt(token).key
        return jwt.decode(
            token, signing_key, algorithms=[algorithm], audience="authenticated"
        )

    if algorithm == "HS256":
        if not settings.supabase_jwt_secret:
            raise _unauthorized("HS256 token received but no JWT secret configured")
        return jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )

    raise _unauthorized(f"unsupported token algorithm: {algorithm}")


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedUser:
    """FastAPI dependency: verifies the bearer token, returns the caller."""
    try:
        claims = _decode(credentials.credentials, settings)
    except HTTPException:
        raise
    except jwt.ExpiredSignatureError as exc:
        raise _unauthorized("token expired") from exc
    except jwt.PyJWTError as exc:
        raise _unauthorized("token verification failed") from exc

    subject = claims.get("sub")
    if not subject:
        raise _unauthorized("token has no subject")

    return AuthenticatedUser(
        user_id=subject,
        email=claims.get("email"),
        phone=claims.get("phone"),
        claims=claims,
    )
