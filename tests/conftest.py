import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import dispose_engine, get_sessionmaker, init_db


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Point every test at its own temporary data directory and database."""
    monkeypatch.chdir(tmp_path)  # so a developer's local .env never leaks into tests
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
async def session(isolated_settings):
    """An async session on a fresh database with all tables created."""
    isolated_settings.ensure_dirs()
    await init_db()
    async with get_sessionmaker()() as db_session:
        yield db_session
    await dispose_engine()


@pytest.fixture
def app():
    from app.main import create_app

    return create_app()


@pytest.fixture
async def api(app, session):
    """Async HTTP client sharing the event loop and database with the `session` fixture."""
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


@pytest.fixture
def client():
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
