"""End-to-end voice approval through the real app, PostgreSQL, worker and webhook path.

Only the phone network is simulated (MockVoiceProvider, Bolna-shaped payloads). Events
enter through the authenticated webhook endpoint like real Bolna deliveries.
"""

import pytest
from sqlalchemy import func, select

from app.db.models import ApprovalVote, AuditEvent
from app.voice.mock import MockBehavior
from tests.conftest import WEBHOOK_TOKEN
from tests.helpers import (
    API,
    create_approver,
    create_policy,
    create_request,
    deliver_webhook,
    list_calls,
    schedule_calls,
)

pytestmark = pytest.mark.integration

ALICE_PHONE, BOB_PHONE = "+14155550101", "+14155550102"


def counts(session):
    session.expire_all()
    votes = session.scalar(select(func.count()).select_from(ApprovalVote))
    audits = session.scalar(select(func.count()).select_from(AuditEvent))
    return votes, audits


def status_of(client, tenant, request_id):
    return client.get(f"{API}/requests/{request_id}", headers=tenant.agent).json()["status"]


def setup_request(client, tenant, summary):
    alice = create_approver(
        client, tenant, "Alice (SRE)", phone_number=ALICE_PHONE, email="alice@example.com"
    )
    bob = create_approver(
        client, tenant, "Bob (Security)", phone_number=BOB_PHONE, email="bob@example.com"
    )
    policy = create_policy(client, tenant, [alice["id"], bob["id"]], name=summary)
    request = create_request(
        client,
        tenant,
        policy["id"],
        summary=summary,
        context={"service": "payments-api", "version": "2.3.0", "risk": "high"},
    )
    return alice, bob, request


def test_two_approver_voice_approval_end_to_end(
    voice_client, make_tenant, mock_provider, run_worker, session
):
    # 1. Organization with least-privilege keys (agent / admin / voice operator).
    tenant = make_tenant("Acme", voice=["voice:calls:create", "voice:calls:read"])
    # 2-4. Two approvers, a two-person policy, a pending request.
    alice, bob, request = setup_request(voice_client, tenant, "Deploy payments-api v2.3.0")
    assert request["status"] == "pending"

    # 5. Schedule calls.
    scheduled = schedule_calls(voice_client, tenant, request["id"])
    assert scheduled.status_code == 201 and scheduled.json()["created"] == 2

    # 6-7. The worker dials exactly the snapshotted approvers, with this request's context.
    run_worker()
    # Both calls go out in parallel; the order they are dialled in is not significant.
    assert sorted(c.recipient_phone_number for c in mock_provider.initiated) == [
        ALICE_PHONE,
        BOB_PHONE,
    ]
    assert all(
        c.variables["request_summary"] == "Deploy payments-api v2.3.0"
        and c.variables["risk_details"] == "risk: high"
        for c in mock_provider.initiated
    )
    alice_call, bob_call = list_calls(voice_client, tenant, request["id"])

    # 8-9. Alice approves on the phone: authenticated webhook -> one vote -> still pending.
    payload = mock_provider.complete(alice_call["provider_call_id"], decision="approve")
    assert deliver_webhook(voice_client, payload, WEBHOOK_TOKEN).json() == {"status": "accepted"}
    run_worker()
    assert status_of(voice_client, tenant, request["id"]) == "pending"

    # 10-11. Bob approves: the deterministic policy engine approves the request.
    payload_bob = mock_provider.complete(bob_call["provider_call_id"], decision="approve")
    deliver_webhook(voice_client, payload_bob, WEBHOOK_TOKEN)
    run_worker()
    detail = voice_client.get(f"{API}/requests/{request['id']}", headers=tenant.agent).json()
    assert detail["status"] == "approved"
    assert {v["approver_id"] for v in detail["votes"]} == {alice["id"], bob["id"]}

    # 12. Redelivery (identical, and with a new timestamp) changes nothing material.
    votes_before, _ = counts(session)
    assert deliver_webhook(voice_client, payload_bob, WEBHOOK_TOKEN).json() == {
        "status": "duplicate"
    }
    deliver_webhook(voice_client, {**payload_bob, "updated_at": "redelivered"}, WEBHOOK_TOKEN)
    run_worker()
    votes_after, _ = counts(session)
    assert votes_after == votes_before == 2
    session.expire_all()
    assert (
        session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == "request.approved")
        )
        == 1
    )

    timeline = [
        e.event_type
        for e in session.scalars(
            select(AuditEvent)
            .where(AuditEvent.request_id == detail["id"])
            .order_by(AuditEvent.sequence)
        )
    ]
    assert timeline[0] == "request.created"
    assert timeline.count("voice.call_scheduled") == 2
    assert timeline.count("voice.call_initiated") == 2
    assert timeline.count("voice.decision_accepted") == 2
    decided = timeline.index("request.approved")
    assert timeline[decided - 1] == "vote.recorded"  # Bob's vote decided it...
    assert timeline[decided + 1] == "voice.decision_accepted"  # ...in the same transaction
    assert timeline[-1] == "voice.event_ignored"  # the timestamped redelivery


def test_rejection_end_to_end(voice_client, make_tenant, mock_provider, run_worker):
    # 13. Separate scenario: first approves, second rejects -> rejected, never approved.
    tenant = make_tenant("Initech", voice=["voice:calls:create", "voice:calls:read"])
    _, _, request = setup_request(voice_client, tenant, "Rotate production database")
    schedule_calls(voice_client, tenant, request["id"])
    run_worker()
    alice_call, bob_call = list_calls(voice_client, tenant, request["id"])
    for call, decision in ((alice_call, "approve"), (bob_call, "reject")):
        payload = mock_provider.complete(call["provider_call_id"], decision=decision)
        deliver_webhook(voice_client, payload, WEBHOOK_TOKEN)
        run_worker()
    assert status_of(voice_client, tenant, request["id"]) == "rejected"


def test_failed_and_ambiguous_calls_never_authorize(
    voice_client, make_tenant, mock_provider, run_worker, session
):
    # 14. One call is never answered, the other is ambiguous: nothing is authorized.
    tenant = make_tenant("Umbrella", voice=["voice:calls:create", "voice:calls:read"])
    _, _, request = setup_request(voice_client, tenant, "Delete staging cluster")
    mock_provider.script(BOB_PHONE, MockBehavior.AMBIGUOUS_TIMEOUT)
    schedule_calls(voice_client, tenant, request["id"])
    run_worker()
    alice_call, bob_call = list_calls(voice_client, tenant, request["id"])
    assert bob_call["status"] == "unknown"

    deliver_webhook(
        voice_client,
        mock_provider.set_status(alice_call["provider_call_id"], "no-answer"),
        WEBHOOK_TOKEN,
    )
    run_worker()
    # An answered call with an unclear answer does not count either.
    redial = schedule_calls(voice_client, tenant, request["id"], redial_unanswered=True)
    assert redial.json()["created"] == 2
    run_worker()
    calls = list_calls(voice_client, tenant, request["id"])
    for call in calls[-2:]:
        if call["provider_call_id"]:
            payload = mock_provider.complete(call["provider_call_id"], decision="uncertain")
            deliver_webhook(voice_client, payload, WEBHOOK_TOKEN)
    run_worker()

    votes, _ = counts(session)
    assert votes == 0
    assert status_of(voice_client, tenant, request["id"]) == "pending"
    outcomes = {c["outcome"] for c in list_calls(voice_client, tenant, request["id"])}
    assert {"no-answer", "initiation_unknown", "uncertain"} <= outcomes
