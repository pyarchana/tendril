import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Point every test at its own temporary data directory and database."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def client():
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
