"""FastAPI application entrypoint.

Started by Render as:
    uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""

from fastapi import FastAPI

from app.api import capabilities, health, me
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

app.include_router(health.router)
app.include_router(capabilities.router)
app.include_router(me.router)
