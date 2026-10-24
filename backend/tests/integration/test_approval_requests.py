from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.models import Approver, AuditEvent
from tests.helpers import API, create_approver, create_policy, create_request, vote

pytestmark = pytest.mark.integration


def test_create_request_snapshots_policy(client, tenant, two_approver_setup, session):
    policy = two_approver_setup["policy"]
    request = create_request(client, tenant, policy["id"], context={"build": "1.4.2"})

    assert request["status"] == "pending"
    assert request["decided_at"] is None
    assert request["required_approvals"] == 2
    assert request["policy_version"] == 1
    assert request["authorized_approver_ids"] == [a["id"] for a in policy["approvers"]]
    assert request["context"] == {"build": "1.4.2"}
    assert request["votes"] == []

    # Policy administration is audited separately (policy_id); look at this request only.
    events = session.scalars(
        select(AuditEvent).where(AuditEvent.request_id == UUID(request["id"]))
    ).all()
    assert [e.event_type for e in events] == ["request.created"]
    assert events[0].event_data["approver_ids"] == request["authorized_approver_ids"]
    assert "build" not in str(events[0].event_data)  # context is not copied into audit


def test_context_defaults_to_empty_object(client, tenant, two_approver_setup):
    request = create_request(client, tenant, two_approver_setup["policy"]["id"])
    assert request["context"] == {}


def test_get_request(client, tenant, two_approver_setup):
    created = create_request(client, tenant, two_approver_setup["policy"]["id"])
    response = client.get(f"{API}/requests/{created['id']}", headers=tenant.agent)
    assert response.status_code == 200
    assert response.json() == created


def test_unknown_request_is_404(client, tenant):
    response = client.get(
        f"{API}/requests/00000000-0000-0000-0000-000000000000", headers=tenant.agent
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "request_not_found"


def test_list_requests_filter_and_order(client, tenant, two_approver_setup):
    setup = two_approver_setup
    first = create_request(client, tenant, setup["policy"]["id"], action="first")
    second = create_request(client, tenant, setup["policy"]["id"], action="second")
    vote(client, tenant, first["id"], setup["alice"]["id"], "reject")

    everything = client.get(f"{API}/requests", headers=tenant.agent).json()
    assert [r["id"] for r in everything["items"]] == [second["id"], first["id"]]
    assert "context" not in everything["items"][0]

    rejected = client.get(f"{API}/requests?status=rejected", headers=tenant.agent).json()
    assert [r["id"] for r in rejected["items"]] == [first["id"]]
    pending = client.get(f"{API}/requests?status=pending", headers=tenant.agent).json()
    assert [r["id"] for r in pending["items"]] == [second["id"]]
    approved = client.get(f"{API}/requests?status=approved", headers=tenant.agent).json()
    assert approved == {"items": [], "total": 0, "limit": 50, "offset": 0}


def test_unknown_policy_is_404(client, tenant):
    response = client.post(
        f"{API}/requests",
        json={"policy_id": "00000000-0000-0000-0000-000000000000", "action": "a", "summary": "s"},
        headers=tenant.agent,
    )
    assert response.status_code == 404


def test_inactive_policy_rejects_new_requests(client, tenant, two_approver_setup):
    policy = two_approver_setup["policy"]
    client.patch(f"{API}/policies/{policy['id']}", json={"is_active": False}, headers=tenant.admin)
    response = client.post(
        f"{API}/requests",
        json={"policy_id": policy["id"], "action": "a", "summary": "s"},
        headers=tenant.agent,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "policy_inactive"


def test_policy_with_deactivated_approver_rejects_new_requests(client, tenant, two_approver_setup):
    bob = two_approver_setup["bob"]
    client.patch(f"{API}/approvers/{bob['id']}", json={"is_active": False}, headers=tenant.admin)
    response = client.post(
        f"{API}/requests",
        json={"policy_id": two_approver_setup["policy"]["id"], "action": "a", "summary": "s"},
        headers=tenant.agent,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "policy_approver_inactive"


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "", "summary": "s"},
        {"action": "   ", "summary": "s"},
        {"action": "a", "summary": "\n\t"},
        {"action": "a" * 201, "summary": "s"},
        {"action": "a", "summary": "s" * 2001},
        {"summary": "s"},
    ],
)
def test_invalid_request_fields(client, tenant, two_approver_setup, payload):
    payload = {"policy_id": two_approver_setup["policy"]["id"], **payload}
    response = client.post(f"{API}/requests", json=payload, headers=tenant.agent)
    assert response.status_code == 422


class TestSnapshotImmutability:
    def test_policy_changes_do_not_affect_existing_request(
        self, client, tenant, two_approver_setup
    ):
        setup = two_approver_setup
        request = create_request(client, tenant, setup["policy"]["id"])
        carol = create_approver(client, tenant, "Carol")

        # Swap Bob for Carol and drop the requirement to a single approval.
        client.patch(
            f"{API}/policies/{setup['policy']['id']}",
            json={"required_approvals": 1, "approver_ids": [carol["id"]]},
            headers=tenant.admin,
        ).raise_for_status()

        # Carol is not in the snapshot, so she cannot vote on the existing request.
        response = vote(client, tenant, request["id"], carol["id"], "approve")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "approver_not_authorized"

        # The original rules still apply: one approval is not enough.
        assert (
            vote(client, tenant, request["id"], setup["alice"]["id"], "approve").json()["request"][
                "status"
            ]
            == "pending"
        )
        fetched = client.get(f"{API}/requests/{request['id']}", headers=tenant.agent).json()
        assert fetched["required_approvals"] == 2 and fetched["policy_version"] == 1
        assert fetched["authorized_approver_ids"] == request["authorized_approver_ids"]

        # New requests use the new version.
        new = create_request(client, tenant, setup["policy"]["id"])
        assert new["policy_version"] == 2
        assert new["authorized_approver_ids"] == [carol["id"]]

    def test_deactivating_policy_does_not_affect_existing_request(
        self, client, tenant, two_approver_setup
    ):
        setup = two_approver_setup
        request = create_request(client, tenant, setup["policy"]["id"])
        client.patch(
            f"{API}/policies/{setup['policy']['id']}",
            json={"is_active": False},
            headers=tenant.admin,
        ).raise_for_status()
        vote(client, tenant, request["id"], setup["alice"]["id"], "approve")
        result = vote(client, tenant, request["id"], setup["bob"]["id"], "approve").json()
        assert result["request"]["status"] == "approved"


class TestAuditTrail:
    def test_audit_events_are_append_only_in_the_database(
        self, client, tenant, two_approver_setup, session
    ):
        request = create_request(client, tenant, two_approver_setup["policy"]["id"])
        for statement in ("UPDATE audit_events SET event_type = 'x'", "DELETE FROM audit_events"):
            with pytest.raises(DBAPIError, match="append-only"):
                session.execute(text(statement))
            session.rollback()
        mine = select(AuditEvent).where(AuditEvent.request_id == UUID(request["id"]))
        assert len(session.scalars(mine).all()) == 1


def test_cannot_create_request_with_other_tenants_policy(client, tenant, make_tenant):
    other = make_tenant("Globex")
    approver = create_approver(client, other, "Mallory")
    foreign_policy = create_policy(client, other, [approver["id"]])
    response = client.post(
        f"{API}/requests",
        json={"policy_id": foreign_policy["id"], "action": "a", "summary": "s"},
        headers=tenant.agent,
    )
    assert response.status_code == 404


def test_database_errors_do_not_expose_bound_values(engine):
    with engine.connect() as connection, pytest.raises(IntegrityError) as caught:
        # Violates the foreign key to organizations; the email must not appear in the error.
        connection.execute(
            Approver.__table__.insert().values(
                organization_id=UUID(int=0),
                name="Leak",
                email="leak-check@example.com",
            )
        )
    assert "leak-check@example.com" not in str(caught.value)
