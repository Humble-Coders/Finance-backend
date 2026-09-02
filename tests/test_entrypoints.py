"""Every deployable entrypoint must import.

CI runs the test suite, but the suite only imports what it directly needs —
`app.core`, `app.db`, `app.models`. Nothing imported `app.main` or
`app.workers.main`, so an import-time failure in either passed CI and failed on
deploy. That is exactly the second of the three deploy failures this project's
CI was created to prevent (`ModuleNotFoundError: psycopg2`).

Linting does not close the gap: ruff does not resolve imports, so a *used*
import of a module that does not exist is invisible to it.

`app.main` configures the FastAPI instance at import (it reads `is_production`
to decide whether to expose /docs), so importing it legitimately requires
settings — unlike `app.models`, which must not. These tests therefore supply
throwaway values rather than weakening `Settings`, which should keep requiring
DATABASE_URL in production. Nothing connects: the engine is created lazily.

The property under test is that the **import graph is intact** — which is what
`ModuleNotFoundError: psycopg2` broke.
"""

from __future__ import annotations

import importlib

import pytest

from app.config import get_settings

@pytest.fixture(autouse=True)
def _throwaway_settings(monkeypatch: pytest.MonkeyPatch):
    """Minimal configuration so the entrypoints can be imported.

    Deliberately not real: nothing here connects. The cache is cleared on both
    sides so these values cannot leak into any other test.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost:5432/postgres")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


DEPLOYED_ENTRYPOINTS = [
    # Render: uvicorn app.main:app
    "app.main",
    # Render: python -m app.workers.main
    "app.workers.main",
]


@pytest.mark.parametrize("module_name", DEPLOYED_ENTRYPOINTS)
def test_entrypoint_imports(module_name: str) -> None:
    importlib.import_module(module_name)


def test_the_api_exposes_an_asgi_app() -> None:
    """`uvicorn app.main:app` needs `app` to exist and be callable.

    Importing the module is not quite enough: renaming the attribute would keep
    the import green and still break the start command.
    """
    from app.main import app

    assert callable(app)


def test_the_worker_exposes_a_main_entrypoint() -> None:
    """`python -m app.workers.main` needs the module to be runnable."""
    import app.workers.main as worker

    assert hasattr(worker, "Worker")
