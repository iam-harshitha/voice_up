"""Durable deadlines: snapshotted expires_at, idempotent expiry, races with votes."""

import threading
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.db.models import ApprovalVote, AuditEvent, VoiceJob
from app.domain.approval_rules import VoteChoice
from app.services import approval_service
from app.services.voice_job_service import run_pending_jobs
from tests.conftest import WEBHOOK_TOKEN
from tests.helpers import (
    age_request,
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

DEADLINE = 3600


@pytest.fixture
def timed(client, tenant):
    alice = create_approver(client, tenant, "Alice")["id"]
    bob = create_approver(client, tenant, "Bob")["id"]
    policy = make_policy(client, tenant, [alice, bob], decision_deadline_seconds=DEADLINE)
    request = create_request(client, tenant, policy["id"])
    return {"id": request["id"], "alice": alice, "bob": bob, "request": request}


def expire_events(session, request_id):
    session.expire_all()
    return session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.request_id == UUID(request_id), AuditEvent.event_type == "request.expired"
        )
    )


def test_deadline_is_snapshotted_with_a_durable_job(timed, session):
    assert timed["request"]["expires_at"] is not None
    job = session.scalars(select(VoiceJob).where(VoiceJob.request_id == UUID(timed["id"]))).one()
    assert job.job_type == "expire_request" and job.status == "pending"


def test_votes_before_the_deadline_count(client, tenant, timed, session_factory, test_settings):
    age_request(session_factory, timed["id"], DEADLINE - 120)
    assert vote(client, tenant, timed["id"], timed["alice"], "approve").status_code == 201
    # The expiry job is not due yet; running the worker changes nothing.
    assert run_pending_jobs(session_factory, None, test_settings, "w") == []
    assert get_request(client, tenant, timed["id"])["status"] == "pending"


def test_after_the_deadline_votes_are_refused_and_the_request_expires(
    client, tenant, timed, session_factory, test_settings, session
):
    vote(client, tenant, timed["id"], timed["alice"], "approve")
    age_request(session_factory, timed["id"], DEADLINE + 1)
    # Even before the job runs, the evaluation reports the deadline and a vote is refused.
    detail = get_request(client, tenant, timed["id"])
    assert detail["evaluation"]["state"] == "expired" and not detail["evaluation"]["authorized"]
    late = vote(client, tenant, timed["id"], timed["bob"], "approve")
    assert late.status_code == 409 and late.json()["error"]["code"] == "request_expired"

    assert run_pending_jobs(session_factory, None, test_settings, "w") == [
        ("expire_request", "now_expired")
    ]
    detail = get_request(client, tenant, timed["id"])
    assert detail["status"] == "expired" and detail["decided_at"] is not None
    assert detail["evaluation"]["reason"] == "deadline_passed"
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(ApprovalVote)) == 1  # no implicit vote
    assert expire_events(session, timed["id"]) == 1


def test_expiry_is_idempotent(client, tenant, timed, session_factory, test_settings, session):
    age_request(session_factory, timed["id"], DEADLINE + 1)
    run_pending_jobs(session_factory, None, test_settings, "w")
    # Re-queue the job by hand (as if a stale worker re-ran it): nothing more happens.
    with session_factory() as db:
        job = db.scalars(select(VoiceJob).where(VoiceJob.request_id == UUID(timed["id"]))).one()
        job.status = "pending"
        db.commit()
    assert run_pending_jobs(session_factory, None, test_settings, "w") == [
        ("expire_request", "already_expired")
    ]
    assert expire_events(session, timed["id"]) == 1


def test_expiry_never_overwrites_a_final_decision(
    client, tenant, timed, session_factory, test_settings
):
    vote(client, tenant, timed["id"], timed["alice"], "reject")
    age_request(session_factory, timed["id"], DEADLINE + 1)
    assert run_pending_jobs(session_factory, None, test_settings, "w") == [
        ("expire_request", "already_rejected")
    ]
    assert get_request(client, tenant, timed["id"])["status"] == "rejected"


def test_early_job_reschedules_itself_to_the_deadline(
    timed, session_factory, test_settings, session
):
    from datetime import timedelta

    from app.db.base import utcnow

    with session_factory() as db:  # make the job due now, before the deadline
        job = db.scalars(select(VoiceJob).where(VoiceJob.request_id == UUID(timed["id"]))).one()
        job.run_after = utcnow() - timedelta(seconds=1)
        db.commit()
    assert run_pending_jobs(session_factory, None, test_settings, "w") == [
        ("expire_request", "not_due")
    ]
    session.expire_all()
    job = session.scalars(select(VoiceJob).where(VoiceJob.request_id == UUID(timed["id"]))).one()
    assert job.status == "pending" and job.run_after > utcnow() + timedelta(seconds=DEADLINE - 60)


@pytest.mark.parametrize("attempt", range(3))
def test_concurrent_vote_and_expiry_are_serialised(
    client, tenant, timed, session_factory, test_settings, session, attempt
):
    """The final approving vote and the expiry job race on the request row lock. Whatever
    wins, the outcome is a single consistent final state with matching audit events."""
    vote(client, tenant, timed["id"], timed["alice"], "approve")
    age_request(session_factory, timed["id"], DEADLINE - 0.5)  # deadline in ~0.5s
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def last_vote():
        with session_factory() as db:
            barrier.wait()
            try:
                approval_service.cast_vote(
                    db,
                    tenant.organization_id,
                    UUID(timed["id"]),
                    UUID(timed["bob"]),
                    VoteChoice.APPROVE,
                )
                results["vote"] = "ok"
            except Exception as exc:  # recorded and asserted below
                results["vote"] = getattr(exc, "code", type(exc).__name__)

    def expiry():
        import time

        barrier.wait()
        time.sleep(0.5)
        results["jobs"] = run_pending_jobs(session_factory, None, test_settings, "w")

    threads = [threading.Thread(target=last_vote), threading.Thread(target=expiry)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    status = get_request(client, tenant, timed["id"])["status"]
    session.expire_all()
    finals = session.scalars(
        select(AuditEvent.event_type).where(
            AuditEvent.request_id == UUID(timed["id"]),
            AuditEvent.event_type.in_(("request.approved", "request.expired")),
        )
    ).all()
    if results["vote"] == "ok":
        assert status == "approved" and finals == ["request.approved"]
    else:
        assert results["vote"] == "request_expired"
        assert status == "expired" and finals == ["request.expired"]


def test_late_voice_approval_cannot_approve_an_expired_request(
    voice_client, make_tenant, mock_provider, run_worker, session_factory, session
):
    tenant = make_tenant(voice=["voice:calls:create", "voice:calls:read"])
    alice = create_approver(voice_client, tenant, "Alice", phone_number="+14155550101")
    policy = make_policy(voice_client, tenant, [alice["id"]], decision_deadline_seconds=600)
    request = create_request(voice_client, tenant, policy["id"])
    schedule_calls(voice_client, tenant, request["id"])
    run_worker()
    call = list_calls(voice_client, tenant, request["id"])[0]
    age_request(session_factory, request["id"], 601)
    run_worker()  # expiry
    assert get_request(voice_client, tenant, request["id"])["status"] == "expired"
    deliver_webhook(voice_client, mock_provider.complete(call["provider_call_id"]), WEBHOOK_TOKEN)
    assert [r for _, r in run_worker()] == ["request_expired"]
    call = list_calls(voice_client, tenant, request["id"])[0]
    assert call["decision_status"] == "refused"
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(ApprovalVote)) == 0
    assert get_request(voice_client, tenant, request["id"])["status"] == "expired"


def test_expiry_and_its_audit_roll_back_together(
    client, tenant, timed, session_factory, test_settings, session, monkeypatch
):
    age_request(session_factory, timed["id"], DEADLINE + 1)
    real = approval_service.audit_service.record_event

    def failing(db, **kwargs):
        if kwargs["event_type"].value == "request.expired":
            raise RuntimeError("injected failure")
        return real(db, **kwargs)

    monkeypatch.setattr(approval_service.audit_service, "record_event", failing)
    assert run_pending_jobs(session_factory, None, test_settings, "w") == [
        ("expire_request", "internal_error")
    ]
    assert get_request(client, tenant, timed["id"])["status"] == "pending"
    assert expire_events(session, timed["id"]) == 0
    monkeypatch.setattr(approval_service.audit_service, "record_event", real)
    from datetime import timedelta

    from app.db.base import utcnow

    assert run_pending_jobs(
        session_factory, None, test_settings, "w", now=utcnow() + timedelta(seconds=600)
    ) == [("expire_request", "now_expired")]
    assert expire_events(session, timed["id"]) == 1
