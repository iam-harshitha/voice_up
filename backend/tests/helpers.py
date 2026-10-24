"""Test helpers: tenant credentials and thin wrappers around the public API."""

from dataclasses import dataclass, field
from uuid import UUID

from fastapi.testclient import TestClient


@dataclass
class Tenant:
    organization_id: UUID
    keys: dict[str, str] = field(default_factory=dict)
    key_ids: dict[str, UUID] = field(default_factory=dict)

    def headers(self, role: str) -> dict[str, str]:
        return {"X-API-Key": self.keys[role]}

    @property
    def admin(self) -> dict[str, str]:
        return self.headers("admin")

    @property
    def agent(self) -> dict[str, str]:
        return self.headers("agent")

    @property
    def voter(self) -> dict[str, str]:
        return self.headers("voter")


API = "/api/v1"


def create_approver(
    client: TestClient, tenant: Tenant, name: str = "Alice", **fields: object
) -> dict:
    payload = {"name": name, "email": f"{name.lower()}@example.com", **fields}
    response = client.post(f"{API}/approvers", json=payload, headers=tenant.admin)
    assert response.status_code == 201, response.text
    return response.json()


def create_policy(
    client: TestClient, tenant: Tenant, approver_ids: list[str], name: str = "Deploys"
) -> dict:
    response = client.post(
        f"{API}/policies",
        json={
            "name": name,
            "required_approvals": len(approver_ids),
            "approver_ids": approver_ids,
        },
        headers=tenant.admin,
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_request(client: TestClient, tenant: Tenant, policy_id: str, **fields: object) -> dict:
    payload = {
        "policy_id": policy_id,
        "action": "deploy.production",
        "summary": "Deploy build 1.4.2 to production",
        **fields,
    }
    response = client.post(f"{API}/requests", json=payload, headers=tenant.agent)
    assert response.status_code == 201, response.text
    return response.json()


def vote(client: TestClient, tenant: Tenant, request_id: str, approver_id: str, choice: str):
    return client.post(
        f"{API}/requests/{request_id}/votes",
        json={"approver_id": approver_id, "vote": choice},
        headers=tenant.voter,
    )


# --- Voice helpers ---------------------------------------------------------------------


def schedule_calls(client: TestClient, tenant: Tenant, request_id: str, **body: object):
    return client.post(
        f"{API}/requests/{request_id}/voice-calls",
        json=body or None,
        headers=tenant.headers("voice"),
    )


def list_calls(client: TestClient, tenant: Tenant, request_id: str) -> list[dict]:
    response = client.get(
        f"{API}/requests/{request_id}/voice-calls", headers=tenant.headers("voice")
    )
    assert response.status_code == 200, response.text
    return response.json()["calls"]


def deliver_webhook(client: TestClient, payload: object, token: str | None, **headers: str):
    sent = {"Content-Type": "application/json", **headers}
    if token is not None:
        sent["X-Approval-Line-Webhook-Token"] = token
    import json

    return client.post(f"{API}/voice/webhooks/bolna", content=json.dumps(payload), headers=sent)


# --- Chunk 3 helpers -------------------------------------------------------------------


def post_policy(client: TestClient, tenant: Tenant, approver_ids: list[str], **fields: object):
    body = {
        "name": fields.pop("name", "Policy"),
        "required_approvals": fields.pop("required_approvals", len(approver_ids)),
        "approver_ids": approver_ids,
        **fields,
    }
    return client.post(f"{API}/policies", json=body, headers=tenant.admin)


def make_policy(client: TestClient, tenant: Tenant, approver_ids: list[str], **fields) -> dict:
    response = post_policy(client, tenant, approver_ids, **fields)
    assert response.status_code == 201, response.text
    return response.json()


def get_request(client: TestClient, tenant: Tenant, request_id: str) -> dict:
    response = client.get(f"{API}/requests/{request_id}", headers=tenant.agent)
    assert response.status_code == 200, response.text
    return response.json()


def age_request(session_factory, request_id: str, seconds: float) -> None:
    """Simulate time passing: move the request's clock and its jobs into the past."""
    from sqlalchemy import text

    with session_factory() as db:
        db.execute(
            text(
                "UPDATE approval_requests SET created_at = created_at - make_interval(secs => :s),"
                " expires_at = expires_at - make_interval(secs => :s) WHERE id = :id"
            ),
            {"s": seconds, "id": request_id},
        )
        db.execute(
            text(
                "UPDATE voice_jobs SET run_after = run_after - make_interval(secs => :s)"
                " WHERE request_id = :id"
            ),
            {"s": seconds, "id": request_id},
        )
        db.commit()


def audit_types(client: TestClient, tenant: Tenant, request_id: str) -> list[str]:
    response = client.get(
        f"{API}/requests/{request_id}/audit-events?limit=200", headers=tenant.headers("auditor")
    )
    assert response.status_code == 200, response.text
    return [e["event_type"] for e in response.json()["items"]]
