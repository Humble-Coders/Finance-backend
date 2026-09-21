"""Error handlers that are policy rather than plumbing.

Separate from `app.main` so importing the policy does not import the
application — `app.main` builds the app at import time, which needs settings,
which a test of this handler has no business requiring.
"""

from __future__ import annotations

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

__all__ = ["validation_error"]

# Pydantic attaches the offending value to every error it reports. Useful in
# most APIs; here it is the statement itself.
_UNSAFE_KEYS = ("input", "ctx")


async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """422s that name the field, and never quote the value.

    FastAPI's default handler returns Pydantic's errors verbatim, and those
    carry an `input` key holding the value that failed. For most APIs that is a
    convenience; for this one it means a malformed statement import sends the
    statement back in the response body — and from there into logs, error
    trackers and anywhere else a 4xx body travels. The field path is what a
    client needs in order to fix the request; the value is what we promised not
    to keep.
    """
    safe = [
        {key: value for key, value in error.items() if key not in _UNSAFE_KEYS}
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": safe}
    )
