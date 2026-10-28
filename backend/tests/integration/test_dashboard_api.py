"""Chunk 4 read-model additions: request detail snapshot, list evaluations, stats, recent
audit events. All derived from the existing engine and persisted state."""

import pytest

from tests.helpers import (
    API,
    age_request,
    create_approver,
    create_request,
    get_request,
    make_policy,
    vote,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def org(client, make_tenant):
    tenant = make_tenant(resolver=["conditions:resolve"], auditor=["audit:read"])
    people = {
        n: create_approver(client, tenant, n, phone_number=f"+1415555016{i}")["id"]
        for i, n in enumerate(("Alice", "Bob", "Backup"))
    }
    policy = make_policy(
        client,
        tenant,
        [people["Alice"], people["Bob"]],
        veto_mode="any_rejection",
        backup_approver_ids=[people["Backup"]],
        escalation_after_seconds=600,
        decision_deadline_seconds=3600,
    )
    return {"tenant": tenant, "policy": policy, **people}


def test_request_detail_includes_snapshot_rules_and_named_seats(client, org):
    request = create_request(client, org["tenant"], org["policy"]["id"])
    detail = get_request(client, org["tenant"], request["id"])
    policy = detail["policy"]
    assert policy["schema_version"] == 2
    assert (policy["approval_mode"], policy["required_approvals"], policy["veto_mode"]) == (
        "unanimous",
        2,
        "any_rejection",
    )
    assert (policy["deadline_seconds"], policy["escalation_after_seconds"]) == (3600, 600)
    assert [(a["name"], a["role"]) for a in detail["approvers"]] == [
        ("Alice", "primary"),
        ("Bob", "primary"),
        ("Backup", "backup"),
    ]
    # No contact details anywhere in the request detail.
    assert "+1415555016" not in str(detail) and "@example.com" not in str(detail)


def test_snapshot_survives_policy_edits(client, org):
    request = create_request(client, org["tenant"], org["policy"]["id"])
    client.patch(
        f"{API}/policies/{org['policy']['id']}",
        json={"name": "Renamed", "decision_deadline_seconds": 7200},
        headers=org["tenant"].admin,
    ).raise_for_status()
    detail = get_request(client, org["tenant"], request["id"])
    assert detail["policy"]["deadline_seconds"] == 3600 and detail["policy"]["version"] == 1


def test_list_evaluation_is_opt_in_and_authoritative(client, org):
    request = create_request(client, org["tenant"], org["policy"]["id"])
    vote(client, org["tenant"], request["id"], org["Alice"], "approve")
    plain = client.get(f"{API}/requests", headers=org["tenant"].agent).json()["items"][0]
    assert plain["evaluation"] is None
    rich = client.get(
        f"{API}/requests?include_evaluation=true", headers=org["tenant"].agent
    ).json()["items"][0]
    evaluation = rich["evaluation"]
    # One approval of two: still not authorized.
    assert (evaluation["approvals"], evaluation["required_approvals"]) == (1, 2)
    assert evaluation["authorized"] is False and rich["status"] == "pending"


def test_stats_are_exact_and_use_the_engine(client, org, session_factory):
    tenant = org["tenant"]
    approved = create_request(client, tenant, org["policy"]["id"])
    vote(client, tenant, approved["id"], org["Alice"], "approve")
    vote(client, tenant, approved["id"], org["Bob"], "approve")
    rejected = create_request(client, tenant, org["policy"]["id"])
    vote(client, tenant, rejected["id"], org["Alice"], "reject")
    conditioned = create_request(client, tenant, org["policy"]["id"])
    client.post(
        f"{API}/requests/{conditioned['id']}/conditions",
        json={"approver_id": org["Alice"], "description": "after backup check"},
        headers=tenant.voter,
    ).raise_for_status()
    reapproval = create_request(client, tenant, org["policy"]["id"])
    condition = client.post(
        f"{API}/requests/{reapproval['id']}/conditions",
        json={"approver_id": org["Bob"], "description": "after QA"},
        headers=tenant.voter,
    ).json()["condition"]
    client.post(
        f"{API}/requests/{reapproval['id']}/conditions/{condition['id']}/resolve",
        json={"outcome": "satisfied", "note": "QA passed"},
        headers=tenant.headers("resolver"),
    ).raise_for_status()
    overdue = create_request(client, tenant, org["policy"]["id"])
    age_request(session_factory, overdue["id"], 3601)
    create_request(client, tenant, org["policy"]["id"])  # plain pending

    stats = client.get(f"{API}/requests/stats", headers=tenant.agent)
    assert stats.status_code == 200
    body = stats.json()
    assert body["total"] == 6
    assert body["by_status"] == {"pending": 4, "approved": 1, "rejected": 1, "expired": 0}
    assert body["pending_waiting_for_condition"] == 1
    assert body["pending_awaiting_reapproval"] == 1
    assert body["pending_deadline_passed"] == 1
    assert body["pending_evaluated"] == 4 and body["evaluation_complete"] is True


def test_stats_are_organization_scoped_and_need_read_scope(client, org, make_tenant):
    create_request(client, org["tenant"], org["policy"]["id"])
    other = make_tenant("Globex")
    assert client.get(f"{API}/requests/stats", headers=other.agent).json()["total"] == 0
    no_read = make_tenant("Initech", writer=["requests:create"])
    assert client.get(f"{API}/requests/stats", headers=no_read.headers("writer")).status_code == 403


def test_recent_audit_events_are_newest_first_and_scoped(client, org, make_tenant):
    tenant = org["tenant"]
    request = create_request(client, tenant, org["policy"]["id"])
    vote(client, tenant, request["id"], org["Alice"], "reject")
    response = client.get(f"{API}/audit-events/recent?limit=3", headers=tenant.headers("auditor"))
    assert response.status_code == 200
    items = response.json()["items"]
    assert [e["event_type"] for e in items] == [
        "request.rejected",
        "vote.recorded",
        "request.created",
    ]
    assert items[0]["request_id"] == request["id"]
    sequences = [e["sequence"] for e in items]
    assert sequences == sorted(sequences, reverse=True)
    other = make_tenant("Globex", auditor=["audit:read"])
    assert (
        client.get(f"{API}/audit-events/recent", headers=other.headers("auditor")).json()["items"]
        == []
    )
    assert client.get(f"{API}/audit-events/recent", headers=tenant.agent).status_code == 403


@pytest.mark.parametrize("limit", [0, 51])
def test_recent_audit_events_are_bounded(client, org, limit):
    response = client.get(
        f"{API}/audit-events/recent?limit={limit}", headers=org["tenant"].headers("auditor")
    )
    assert response.status_code == 422


def test_policy_audit_events_carry_the_policy_id(client, org):
    events = client.get(
        f"{API}/policies/{org['policy']['id']}/audit-events",
        headers=org["tenant"].headers("auditor"),
    ).json()["items"]
    assert events[0]["policy_id"] == org["policy"]["id"] and events[0]["request_id"] is None
