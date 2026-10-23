"""Contract tests for the HTTP surface: error shape, auth ordering, schemas, config."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.security import AGENT_SCOPES, generate_api_key, hash_api_key, parse_scopes
from app.main import create_app
from tests.api.test_health import UNREACHABLE_DB
from tests.helpers import API, create_request, vote

PROTECTED = [
    ("GET", "/approvers"),
    ("POST", "/approvers"),
    ("GET", "/policies"),
    ("POST", "/policies"),
    ("GET", f"/policies/{uuid4()}"),
    ("PATCH", f"/policies/{uuid4()}"),
    ("GET", "/requests"),
    ("POST", "/requests"),
    ("GET", f"/requests/{uuid4()}"),
    ("POST", f"/requests/{uuid4()}/votes"),
]


@pytest.fixture
def offline_client():
    settings = Settings(database_url=UNREACHABLE_DB, app_env="test", log_level="WARNING")
    app = create_app(settings)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("internal detail: password=hunter2")

    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_missing_api_key_is_401_before_body_validation(offline_client, method, path):
    # Invalid bodies must not be validated (or echoed) for unauthenticated callers.
    response = offline_client.request(method, API + path, json={"junk": True})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "api_key_missing"
    assert response.headers["www-authenticate"] == "APIKey"


def test_oversized_api_key_is_rejected_without_lookup(offline_client):
    response = offline_client.get(f"{API}/requests", headers={"X-API-Key": "x" * 300})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "api_key_invalid"


def test_unknown_route_uses_error_format(offline_client):
    response = offline_client.get(f"{API}/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_wrong_method_uses_error_format(offline_client):
    response = offline_client.delete(f"{API}/health/live")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_unhandled_exception_is_generic_500(offline_client):
    response = offline_client.get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["request_id"] == response.headers["x-request-id"]
    assert "hunter2" not in response.text and "RuntimeError" not in response.text


def test_request_create_schema_forbids_server_derived_fields(offline_client):
    schema = offline_client.get("/openapi.json").json()["components"]["schemas"]
    create = schema["ApprovalRequestCreate"]
    assert create["additionalProperties"] is False
    assert set(create["properties"]) == {"policy_id", "action", "summary", "context"}
    assert create["properties"]["context"]["type"] == "object"


def test_docs_are_disabled_in_production():
    settings = Settings(database_url=UNREACHABLE_DB, app_env="production", log_level="WARNING")
    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


# --- Configuration -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@localhost/db",  # psycopg2 dialect, not psycopg 3
        "sqlite:///approval.db",
        "postgresql+psycopg://u:p@localhost",  # no database name
        "not a url",
    ],
)
def test_invalid_database_url_fails_fast(url):
    with pytest.raises(ValidationError):
        Settings(database_url=url)


def test_missing_database_url_fails_fast(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env file here
    with pytest.raises(ValidationError):
        Settings()


def test_cors_origins_parsing_and_wildcard_rejection():
    settings = Settings(database_url=UNREACHABLE_DB, cors_origins="http://a.test, http://b.test")
    assert settings.cors_origins == ["http://a.test", "http://b.test"]
    with pytest.raises(ValidationError):
        Settings(database_url=UNREACHABLE_DB, cors_origins="*")


# --- Credentials -------------------------------------------------------------------------


def test_generated_keys_are_unique_and_high_entropy():
    keys = {generate_api_key() for _ in range(200)}
    assert len(keys) == 200
    assert all(key.startswith("al_") and len(key) >= 46 for key in keys)


def test_hash_is_sha256_hex_and_not_the_key():
    key = generate_api_key()
    digest = hash_api_key(key)
    assert len(digest) == 64 and key not in digest
    assert digest == hash_api_key(key)


def test_parse_scopes():
    assert parse_scopes("requests:read, requests:create") == sorted(AGENT_SCOPES)
    with pytest.raises(ValueError, match="unknown scope"):
        parse_scopes("requests:read,admin")
    with pytest.raises(ValueError):
        parse_scopes(" , ")


def test_agent_scopes_cannot_vote_or_change_policy():
    assert {"requests:create", "requests:read"} == AGENT_SCOPES


# --- Contracts that need a database ------------------------------------------------------


@pytest.mark.integration
class TestWithDatabase:
    def test_validation_error_shape_and_no_input_echo(self, client, tenant):
        response = client.post(
            f"{API}/approvers",
            json={"name": "  ", "email": "secret-not-an-email"},
            headers=tenant.admin,
        )
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "validation_error"
        assert {tuple(detail["loc"]) for detail in error["details"]} >= {
            ("body", "name"),
            ("body", "email"),
        }
        assert "secret-not-an-email" not in response.text

    @pytest.mark.parametrize(
        "extra",
        [
            {"status": "approved"},
            {"organization_id": str(uuid4())},
            {"policy_snapshot": {"required_approvals": 0}},
            {"decided_at": "2026-01-01T00:00:00Z"},
            {"votes": 2},
        ],
    )
    def test_request_create_rejects_server_derived_fields(
        self, client, tenant, two_approver_setup, extra
    ):
        payload = {
            "policy_id": two_approver_setup["policy"]["id"],
            "action": "deploy",
            "summary": "Deploy",
            **extra,
        }
        response = client.post(f"{API}/requests", json=payload, headers=tenant.agent)
        assert response.status_code == 422
        assert client.get(f"{API}/requests", headers=tenant.agent).json()["total"] == 0

    @pytest.mark.parametrize("context", [[1, 2], "text", 5, None])
    def test_context_must_be_an_object(self, client, tenant, two_approver_setup, context):
        payload = {
            "policy_id": two_approver_setup["policy"]["id"],
            "action": "deploy",
            "summary": "Deploy",
            "context": context,
        }
        response = client.post(f"{API}/requests", json=payload, headers=tenant.agent)
        assert response.status_code == 422

    def test_oversized_context_rejected(self, client, tenant, two_approver_setup):
        payload = {
            "policy_id": two_approver_setup["policy"]["id"],
            "action": "deploy",
            "summary": "Deploy",
            "context": {"blob": "x" * 20_000},
        }
        response = client.post(f"{API}/requests", json=payload, headers=tenant.agent)
        assert response.status_code == 422

    def test_vote_rejects_unknown_fields_and_bad_enum(self, client, tenant, two_approver_setup):
        request = create_request(client, tenant, two_approver_setup["policy"]["id"])
        url = f"{API}/requests/{request['id']}/votes"
        alice = two_approver_setup["alice"]["id"]
        for body in (
            {"approver_id": alice, "vote": "approve", "status": "approved"},
            {"approver_id": alice, "vote": "maybe"},
            {"approver_id": "not-a-uuid", "vote": "approve"},
        ):
            assert client.post(url, json=body, headers=tenant.voter).status_code == 422

    @pytest.mark.parametrize(
        "query", ["limit=0", "limit=101", "offset=-1", "offset=10001", "status=done"]
    )
    def test_pagination_and_filter_bounds(self, client, tenant, query):
        response = client.get(f"{API}/requests?{query}", headers=tenant.agent)
        assert response.status_code == 422

    def test_pagination_shape(self, client, tenant, two_approver_setup):
        policy_id = two_approver_setup["policy"]["id"]
        for _ in range(3):
            create_request(client, tenant, policy_id)
        page = client.get(f"{API}/requests?limit=2&offset=0", headers=tenant.agent).json()
        assert (page["total"], page["limit"], page["offset"], len(page["items"])) == (3, 2, 0, 2)
        rest = client.get(f"{API}/requests?limit=2&offset=2", headers=tenant.agent).json()
        assert len(rest["items"]) == 1
        seen = {item["id"] for item in page["items"] + rest["items"]}
        assert len(seen) == 3

    def test_malformed_path_uuid_is_422(self, client, tenant):
        response = client.get(f"{API}/requests/not-a-uuid", headers=tenant.agent)
        assert response.status_code == 422

    def test_responses_never_contain_secrets(self, client, tenant, two_approver_setup):
        request = create_request(client, tenant, two_approver_setup["policy"]["id"])
        vote(client, tenant, request["id"], two_approver_setup["alice"]["id"], "approve")
        bodies = [
            client.get(f"{API}/requests/{request['id']}", headers=tenant.agent).text,
            client.get(f"{API}/requests", headers=tenant.agent).text,
            client.get(f"{API}/policies", headers=tenant.admin).text,
            client.get(f"{API}/approvers", headers=tenant.admin).text,
        ]
        for body in bodies:
            for key in tenant.keys.values():
                assert key not in body
            assert "key_hash" not in body and "organization_id" not in body
