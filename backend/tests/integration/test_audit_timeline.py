"""Audit timeline API and reconstruction of the flagship two-approver voice scenario."""

import pytest

from tests.conftest import WEBHOOK_TOKEN
from tests.helpers import (
    API,
    create_approver,
    create_request,
    deliver_webhook,
    get_request,
    list_calls,
    make_policy,
    schedule_calls,
    vote,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def audited(client, make_tenant):
    tenant = make_tenant(auditor=["audit:read"])
    a = create_approver(client, tenant, "A")["id"]
    b = create_approver(client, tenant, "B")["id"]
    policy = make_policy(client, tenant, [a, b])
    request = create_request(client, tenant, policy["id"])
    vote(client, tenant, request["id"], a, "approve")
    vote(client, tenant, request["id"], b, "approve")
    return {"tenant": tenant, "id": request["id"]}


def timeline(client, tenant, request_id, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(
        f"{API}/requests/{request_id}/audit-events?{query}", headers=tenant.headers("auditor")
    )


def test_timeline_is_ordered_and_paginated_stably(client, audited):
    full = timeline(client, audited["tenant"], audited["id"]).json()
    types = [e["event_type"] for e in full["items"]]
    assert types == ["request.created", "vote.recorded", "vote.recorded", "request.approved"]
    sequences = [e["sequence"] for e in full["items"]]
    assert sequences == sorted(sequences) and full["next_after"] is None

    page1 = timeline(client, audited["tenant"], audited["id"], limit=3).json()
    assert len(page1["items"]) == 3 and page1["next_after"] == sequences[2]
    page2 = timeline(
        client, audited["tenant"], audited["id"], limit=3, after=page1["next_after"]
    ).json()
    assert [e["sequence"] for e in page1["items"] + page2["items"]] == sequences
    assert page2["next_after"] is None


@pytest.mark.parametrize("role", ["agent", "admin", "voter"])
def test_timeline_requires_audit_scope(client, audited, role):
    response = client.get(
        f"{API}/requests/{audited['id']}/audit-events", headers=audited["tenant"].headers(role)
    )
    assert response.status_code == 403


def test_timeline_is_organization_scoped(client, audited, make_tenant):
    other = make_tenant("Globex", auditor=["audit:read"])
    assert timeline(client, other, audited["id"]).status_code == 404


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"after": -1}])
def test_timeline_bounds(client, audited, params):
    assert timeline(client, audited["tenant"], audited["id"], **params).status_code == 422


def test_timeline_is_read_only(client, audited):
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)(
            f"{API}/requests/{audited['id']}/audit-events",
            headers=audited["tenant"].headers("auditor"),
        )
        assert response.status_code == 405


def test_default_agent_scopes_remain_least_privileged():
    from app.core.security import AGENT_SCOPES

    assert {"requests:create", "requests:read"} == AGENT_SCOPES


def test_flagship_two_approver_voice_scenario_is_reconstructable(
    voice_client, make_tenant, mock_provider, run_worker
):
    """First approver approves by phone, second states a condition; nothing is authorized
    until the condition is resolved and the second approver re-approves."""
    tenant = make_tenant(
        voice=["voice:calls:create", "voice:calls:read"],
        resolver=["conditions:resolve"],
        auditor=["audit:read"],
    )
    alice = create_approver(voice_client, tenant, "Alice", phone_number="+14155550101")["id"]
    bob = create_approver(voice_client, tenant, "Bob", phone_number="+14155550102")["id"]
    policy = make_policy(voice_client, tenant, [alice, bob], decision_deadline_seconds=3600)
    request = create_request(
        voice_client, tenant, policy["id"], summary="Deploy payments-api v2.3.0 to production"
    )
    schedule_calls(voice_client, tenant, request["id"])
    run_worker()
    alice_call, bob_call = list_calls(voice_client, tenant, request["id"])

    deliver_webhook(
        voice_client, mock_provider.complete(alice_call["provider_call_id"]), WEBHOOK_TOKEN
    )
    run_worker()
    state = get_request(voice_client, tenant, request["id"])
    assert state["status"] == "pending" and state["evaluation"]["outstanding_approver_ids"] == [bob]

    deliver_webhook(
        voice_client,
        mock_provider.complete(
            bob_call["provider_call_id"],
            decision="conditional_approve",
            condition="only after the database backup is verified",
        ),
        WEBHOOK_TOKEN,
    )
    run_worker()
    state = get_request(voice_client, tenant, request["id"])
    assert state["status"] == "pending" and state["evaluation"]["authorized"] is False
    assert state["evaluation"]["state"] == "waiting_for_condition"

    condition = voice_client.get(
        f"{API}/requests/{request['id']}/conditions", headers=tenant.agent
    ).json()[0]
    voice_client.post(
        f"{API}/requests/{request['id']}/conditions/{condition['id']}/resolve",
        json={"outcome": "satisfied", "note": "backup verified", "evidence_reference": "CHG-1042"},
        headers=tenant.headers("resolver"),
    ).raise_for_status()
    assert get_request(voice_client, tenant, request["id"])["status"] == "pending"
    run_worker()  # dial Bob again for an unconditional answer
    recall = list_calls(voice_client, tenant, request["id"])[-1]
    deliver_webhook(voice_client, mock_provider.complete(recall["provider_call_id"]), WEBHOOK_TOKEN)
    run_worker()
    assert get_request(voice_client, tenant, request["id"])["status"] == "approved"

    events = timeline(voice_client, tenant, request["id"], limit=200).json()["items"]
    types = [e["event_type"] for e in events]
    expected_order = [
        "request.created",
        "voice.call_scheduled",
        "voice.call_initiated",
        "vote.recorded",
        "condition.created",
        "condition.resolved",
        "condition.reapproval_requested",
        "request.approved",
    ]
    positions = [types.index(t) for t in expected_order]
    assert positions == sorted(positions)
    assert types.count("vote.recorded") == 2 and types.count("request.approved") == 1
    resolved = next(e for e in events if e["event_type"] == "condition.resolved")
    assert resolved["event_data"]["evidence_reference"] == "CHG-1042"
    body = str(events)
    for secret in (
        WEBHOOK_TOKEN,
        "+14155550101",
        "+14155550102",
        "simulated conversation",
        "only after the database backup",
    ):
        assert secret not in body
    for key in tenant.keys.values():
        assert key not in body
