from uuid import UUID

import pytest

from app.core.security import hash_api_key
from app.db.models import ApiKey
from app.db.session import transaction
from app.services import api_key_service
from tests.helpers import API, create_approver, create_policy, create_request

pytestmark = pytest.mark.integration


def test_valid_key_is_accepted(client, tenant):
    assert client.get(f"{API}/requests", headers=tenant.agent).status_code == 200


def test_invalid_key_is_401(client, tenant):
    response = client.get(f"{API}/requests", headers={"X-API-Key": "al_" + "x" * 43})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "api_key_invalid"


def test_revoked_key_is_401_with_same_response_as_unknown(client, tenant, session):
    with transaction(session):
        api_key_service.revoke_api_key(session, tenant.key_ids["agent"])
    response = client.get(f"{API}/requests", headers=tenant.agent)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "api_key_invalid"


def test_only_the_hash_is_stored(tenant, session):
    stored = session.get(ApiKey, tenant.key_ids["agent"])
    assert stored.key_hash == hash_api_key(tenant.keys["agent"])
    for column in ApiKey.__table__.columns:
        assert getattr(stored, column.key) != tenant.keys["agent"]


@pytest.mark.parametrize(
    ("role", "method", "path", "body"),
    [
        ("agent", "POST", "/policies", {"name": "x", "required_approvals": 1, "approver_ids": []}),
        ("agent", "POST", "/approvers", {"name": "x", "email": "x@example.com"}),
        ("agent", "GET", "/policies", None),
        ("agent", "GET", "/approvers", None),
        (
            "agent",
            "POST",
            "/requests/00000000-0000-0000-0000-000000000000/votes",
            {"approver_id": "00000000-0000-0000-0000-000000000000", "vote": "approve"},
        ),
        ("voter", "GET", "/requests", None),
        (
            "voter",
            "POST",
            "/requests",
            {"policy_id": "00000000-0000-0000-0000-000000000000", "action": "a", "summary": "s"},
        ),
        (
            "admin",
            "POST",
            "/requests/00000000-0000-0000-0000-000000000000/votes",
            {"approver_id": "00000000-0000-0000-0000-000000000000", "vote": "approve"},
        ),
        (
            "admin",
            "POST",
            "/requests",
            {"policy_id": "00000000-0000-0000-0000-000000000000", "action": "a", "summary": "s"},
        ),
    ],
)
def test_missing_scope_is_403(client, tenant, role, method, path, body):
    response = client.request(method, API + path, json=body, headers=tenant.headers(role))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"


def test_read_only_policy_key_cannot_write(client, make_tenant):
    tenant = make_tenant(reader=["policies:read"])
    response = client.post(
        f"{API}/policies",
        json={"name": "x", "required_approvals": 1, "approver_ids": [str(tenant.organization_id)]},
        headers=tenant.headers("reader"),
    )
    assert response.status_code == 403
    assert client.get(f"{API}/policies", headers=tenant.headers("reader")).status_code == 200


class TestOrganizationIsolation:
    @pytest.fixture
    def tenants(self, client, make_tenant):
        acme, globex = make_tenant("Acme"), make_tenant("Globex")
        approver = create_approver(client, acme, "Alice")
        policy = create_policy(client, acme, [approver["id"]])
        request = create_request(client, acme, policy["id"])
        return acme, globex, approver, policy, request

    def test_other_tenant_sees_nothing(self, client, tenants):
        _, globex, approver, policy, request = tenants
        assert client.get(f"{API}/requests", headers=globex.agent).json()["total"] == 0
        assert client.get(f"{API}/policies", headers=globex.admin).json()["total"] == 0
        assert client.get(f"{API}/approvers", headers=globex.admin).json()["total"] == 0
        for path in (
            f"/requests/{request['id']}",
            f"/policies/{policy['id']}",
            f"/approvers/{approver['id']}",
        ):
            headers = globex.agent if path.startswith("/requests") else globex.admin
            response = client.get(API + path, headers=headers)
            assert response.status_code == 404, path

    def test_other_tenant_cannot_use_policy_or_vote(self, client, tenants):
        _, globex, approver, policy, request = tenants
        response = client.post(
            f"{API}/requests",
            json={"policy_id": policy["id"], "action": "a", "summary": "s"},
            headers=globex.agent,
        )
        assert response.status_code == 404
        response = client.post(
            f"{API}/requests/{request['id']}/votes",
            json={"approver_id": approver["id"], "vote": "approve"},
            headers=globex.voter,
        )
        assert response.status_code == 404

    def test_other_tenant_cannot_modify(self, client, tenants):
        _, globex, approver, policy, _ = tenants
        assert (
            client.patch(
                f"{API}/policies/{policy['id']}", json={"is_active": False}, headers=globex.admin
            ).status_code
            == 404
        )
        assert (
            client.patch(
                f"{API}/approvers/{approver['id']}", json={"is_active": False}, headers=globex.admin
            ).status_code
            == 404
        )

    def test_tenant_comes_from_key_not_payload(self, client, tenants):
        acme, globex, *_ = tenants
        response = client.post(
            f"{API}/approvers",
            json={
                "name": "Mallory",
                "email": "m@example.com",
                "organization_id": str(acme.organization_id),
            },
            headers=globex.admin,
        )
        assert response.status_code == 422


def test_issue_key_for_missing_organization_fails(session):
    with pytest.raises(Exception, match="Organization not found"):
        api_key_service.issue_api_key(
            session, UUID("00000000-0000-0000-0000-000000000000"), "x", "requests:read"
        )
