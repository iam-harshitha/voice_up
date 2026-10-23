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
