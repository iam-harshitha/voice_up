"""Regression: the text channel must not change the voice path.

With TEXT_PROVIDER=disabled and a policy on the default approval_channel ('voice'), an
auto-dial request must create exactly the jobs, snapshot and audit events it created
before the text channel existed (recorded from the pre-text code, commit e95e561):
creation, the worker's dial, a spoken approval, a spoken rejection, and an escalation
round that dials a backup.
"""

from uuid import UUID

import pytest
from sqlalchemy import select

from app.db.models import ApprovalRequest, AuditEvent, VoiceCall, VoiceJob, VoiceProviderEvent
from tests.conftest import WEBHOOK_TOKEN
from tests.helpers import (
    age_request,
    create_approver,
    create_request,
    deliver_webhook,
    list_calls,
    make_policy,
)

pytestmark = pytest.mark.integration

ALICE, BOB, CAROL = "+14155550101", "+14155550102", "+14155550103"


def jobs(session, request_id: str) -> list[tuple[str, str]]:
    """(job_type, status) of the request's jobs, its calls' jobs and their events' jobs."""
    session.expire_all()
    rid = UUID(request_id)
    calls = select(VoiceCall.id).where(VoiceCall.request_id == rid)
    events = select(VoiceProviderEvent.id).where(VoiceProviderEvent.voice_call_id.in_(calls))
    rows = session.execute(
        select(VoiceJob.job_type, VoiceJob.status).where(
            (VoiceJob.request_id == rid)
            | VoiceJob.voice_call_id.in_(calls)
            | VoiceJob.event_id.in_(events)
        )
    ).all()
    return sorted((t, s) for t, s in rows)


def trace(session, request_id: str) -> list[tuple[str, list[str]]]:
    """Audit events in sequence order: (event_type, sorted data keys)."""
    session.expire_all()
    rows = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.request_id == UUID(request_id))
        .order_by(AuditEvent.sequence)
    )
    return [(e.event_type, sorted(e.event_data)) for e in rows]


def snapshot_keys(session, request_id: str) -> list[str]:
    session.expire_all()
    request = session.get(ApprovalRequest, UUID(request_id))
    return sorted(request.policy_snapshot)


@pytest.fixture
def tenant(make_tenant):
    return make_tenant(voice=["voice:calls:create", "voice:calls:read"])


def setup(client, tenant, **policy_fields):
    alice = create_approver(client, tenant, "Alice", phone_number=ALICE)
    bob = create_approver(client, tenant, "Bob", phone_number=BOB)
    carol = create_approver(client, tenant, "Carol", phone_number=CAROL)
    policy = make_policy(
        client,
        tenant,
        [alice["id"], bob["id"]],
        auto_dial=True,
        backup_approver_ids=[carol["id"]],
        decision_deadline_seconds=3600,
        escalation_after_seconds=600,
        **policy_fields,
    )
    return policy, create_request(client, tenant, policy["id"])


SNAPSHOT_KEYS = [
    "approval_mode",
    "approver_ids",
    "backups",
    "condition_rule",
    "deadline_seconds",
    "escalation_after_seconds",
    "max_escalations",
    "name",
    "policy_id",
    "primaries",
    "required_approvals",
    "schema_version",
    "version",
    "veto_mode",
]
CREATED = (
    "request.created",
    [
        "action",
        "approval_mode",
        "approver_ids",
        "auto_dial",
        "backup_approver_ids",
        "escalation_after_seconds",
        "expires_at",
        "max_escalations",
        "policy_id",
        "policy_version",
        "required_approvals",
        "veto_approver_ids",
        "veto_mode",
    ],
)
SCHEDULED = (
    "voice.call_scheduled",
    ["approver_id", "generation", "provider", "reason", "voice_call_id"],
)
AUTO_DIAL = (
    "voice.auto_dial_scheduled",
    ["calls_created", "policy_id", "policy_version", "skipped", "voice_call_ids"],
)
ATTEMPTED = ("voice.call_initiation_attempted", ["attempt", "job_id", "voice_call_id"])
INITIATED = ("voice.call_initiated", ["provider_call_id", "voice_call_id"])


def test_voice_policy_creation_is_unchanged(voice_client, tenant, session):
    policy, request = setup(voice_client, tenant)
    assert policy["approval_channel"] == "voice"
    assert snapshot_keys(session, request["id"]) == SNAPSHOT_KEYS
    assert trace(session, request["id"]) == [CREATED, SCHEDULED, SCHEDULED, AUTO_DIAL]
    assert jobs(session, request["id"]) == [
        ("escalate_request", "pending"),
        ("expire_request", "pending"),
        ("initiate_call", "pending"),
        ("initiate_call", "pending"),
    ]


def test_voice_decisions_are_unchanged(voice_client, tenant, session, mock_provider, run_worker):
    _, request = setup(voice_client, tenant)
    run_worker()
    alice_call, bob_call = list_calls(voice_client, tenant, request["id"])
    for call, decision in ((alice_call, "approve"), (bob_call, "reject")):
        payload = mock_provider.complete(call["provider_call_id"], decision=decision)
        assert deliver_webhook(voice_client, payload, WEBHOOK_TOKEN).status_code == 200
        run_worker()

    observed = trace(session, request["id"])
    assert observed[:4] == [CREATED, SCHEDULED, SCHEDULED, AUTO_DIAL]
    # The dial order of two parallel calls is not significant; the multiset is.
    assert sorted(map(str, observed[4:8])) == sorted(map(str, [ATTEMPTED, INITIATED] * 2))
    assert observed[8:] == RECORDED_DECISION_TAIL
    assert jobs(session, request["id"]) == RECORDED_DECISION_JOBS


def test_voice_escalation_still_dials_the_backup(
    voice_client, tenant, session, run_worker, session_factory
):
    _, request = setup(voice_client, tenant)
    run_worker()
    age_request(session_factory, request["id"], 601)
    run_worker()
    observed = trace(session, request["id"])
    assert observed[8:] == RECORDED_ESCALATION_TAIL
    calls = list_calls(voice_client, tenant, request["id"])
    assert len(calls) == 3  # Alice, Bob and the backup Carol, all by phone


VOTE = (
    "vote.recorded",
    [
        "approvals",
        "approver_id",
        "required_approvals",
        "seat_approver_id",
        "source",
        "voice_call_id",
        "vote",
        "vote_id",
    ],
)
ACCEPTED = ("voice.decision_accepted", ["request_status", "voice_call_id", "vote", "vote_id"])
REJECTED = (
    "request.rejected",
    [
        "approvals",
        "approve_votes",
        "condition_approver_ids",
        "decisive_vote_id",
        "outstanding_approver_ids",
        "reapproval_approver_ids",
        "reapproval_seat_ids",
        "reason",
        "reject_votes",
        "rejections",
        "required_approvals",
        "state",
        "veto_triggered",
    ],
)
RECORDED_DECISION_TAIL = [VOTE, ACCEPTED, VOTE, REJECTED, ACCEPTED]
# The deadline and escalation jobs stay pending; they finish themselves when they run.
RECORDED_DECISION_JOBS = [
    ("escalate_request", "pending"),
    ("expire_request", "pending"),
    ("initiate_call", "succeeded"),
    ("initiate_call", "succeeded"),
    ("process_event", "succeeded"),
    ("process_event", "succeeded"),
]
RECORDED_ESCALATION_TAIL = [
    SCHEDULED,
    (
        "escalation.step_created",
        [
            "backup_approver_id",
            "escalation_step_id",
            "outcome",
            "round",
            "seat_approver_id",
            "sequence",
            "voice_call_id",
        ],
    ),
    ("escalation.exhausted", ["max_escalations", "steps"]),
]
