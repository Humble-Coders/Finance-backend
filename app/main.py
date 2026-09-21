"""FastAPI application entrypoint.

Started by Render as:
    uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import capabilities, financial_setup, health, legal, me, statements
from app.config import get_settings

settings = get_settings()

app = FastAPI(
    title="FinAI API",
    version="0.1.0",
    # No public docs in production — this API serves financial data.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """422s that name the field, and never quote the value.

    FastAPI's default handler returns Pydantic's errors verbatim, and those
    carry an `input` key holding the value that failed. For most APIs that is a
    convenience; for this one it means a malformed statement import would send
    the statement back in the response body — and from there into logs, error
    trackers and anywhere else a 4xx body travels. The field path is what a
    client needs to fix the request; the value is what we promised not to keep.
    """
    safe = [
        {key: value for key, value in error.items() if key not in ("input", "ctx")}
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": safe}
    )


app.include_router(health.router)
app.include_router(capabilities.router)
app.include_router(me.router)
app.include_router(legal.router)
app.include_router(financial_setup.router)
app.include_router(statements.router)
