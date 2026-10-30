from fastapi.testclient import TestClient

from cctv_cloud.main import app
from tests.conftest import reset_settings_caches


def test_healthz():
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_with_database(owner_engine):
    response = TestClient(app).get("/readyz")
    assert response.status_code == 200
    assert response.json()["database"] == "up"


def test_readyz_without_database(monkeypatch):
    monkeypatch.setenv("CCTV_DATABASE_URL", "postgresql+psycopg://nobody@127.0.0.1:1/none")
    monkeypatch.setenv("CCTV_DATABASE_CONNECT_TIMEOUT_SECONDS", "2")
    reset_settings_caches()
    try:
        response = TestClient(app).get("/readyz")
    finally:
        monkeypatch.undo()
        reset_settings_caches()
    assert response.status_code == 503
    assert response.json()["database"] == "down"
