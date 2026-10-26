import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

# Nothing listens on port 1, so connecting is refused immediately.
UNREACHABLE_DB = "postgresql+psycopg://nobody:secret-password@127.0.0.1:1/approval_line_test"


@pytest.fixture
def offline_client():
    settings = Settings(
        _env_file=None,
        database_url=UNREACHABLE_DB,
        app_env="test",
        log_level="WARNING",
        db_connect_timeout_seconds=1,
    )
    with TestClient(create_app(settings)) as client:
        yield client


def test_live_does_not_need_the_database(offline_client):
    response = offline_client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"]


def test_ready_reports_503_without_leaking_details(offline_client):
    response = offline_client.get("/api/v1/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "database_unavailable"
    assert body["error"]["request_id"] == response.headers["x-request-id"]
    for secret in ("secret-password", "nobody", "127.0.0.1", "psycopg", "Traceback"):
        assert secret not in response.text


def test_incoming_request_id_is_propagated(offline_client):
    response = offline_client.get("/api/v1/health/live", headers={"X-Request-ID": "abc-123"})
    assert response.headers["x-request-id"] == "abc-123"


def test_malformed_request_id_is_replaced(offline_client):
    response = offline_client.get("/api/v1/health/live", headers={"X-Request-ID": "bad id!\t"})
    assert response.headers["x-request-id"] != "bad id!\t"


@pytest.mark.integration
def test_ready_with_database(client):
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
