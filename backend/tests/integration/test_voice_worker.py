import threading
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from app.db.base import utcnow
from app.db.models import ApprovalRequest, ApprovalVote, AuditEvent, VoiceCall, VoiceJob
from app.services.voice_job_service import claim_next, schedule_reconciliations
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


class WorkerCrash(BaseException):
    """Simulates the process dying (not an ordinary exception the worker would catch)."""


@pytest.fixture
def scheduled(voice_client, voice_tenant):
    alice = create_approver(voice_client, voice_tenant, "Alice", phone_number=ALICE_PHONE)
    bob = create_approver(voice_client, voice_tenant, "Bob", phone_number=BOB_PHONE)
    policy = create_policy(voice_client, voice_tenant, [alice["id"], bob["id"]])
    request = create_request(voice_client, voice_tenant, policy["id"])
    schedule_calls(voice_client, voice_tenant, request["id"])
    return {"request_id": request["id"], "alice": alice, "bob": bob}


def calls(client, tenant, scheduled):
    return list_calls(client, tenant, scheduled["request_id"])


def count(session, model, *where):
    session.expire_all()
    return session.scalar(select(func.count()).select_from(model).where(*where))


class CrashingProvider:
    """Delegates to the mock but dies inside initiate_call (after phase 1 committed)."""

    def __init__(self, inner):
        self.inner, self.name, self.agent_id = inner, inner.name, inner.agent_id

    def initiate_call(self, call):
        raise WorkerCrash

    def get_execution(self, provider_call_id):
        return self.inner.get_execution(provider_call_id)


class TestCrashRecovery:
    def test_crash_after_claim_is_recovered_when_lease_expires(
        self,
        voice_client,
        voice_tenant,
        scheduled,
        session_factory,
        voice_settings,
        run_worker,
        mock_provider,
    ):
        with session_factory() as db:
            claimed = claim_next(db, voice_settings, "dead-worker")
        assert claimed is not None  # ...and the worker dies before doing anything
        assert [t for t, _ in run_worker()] == ["initiate_call"]  # the other job only
        later = voice_settings.voice_job_lease_seconds + 1
        assert run_worker(later=later) == [("initiate_call", "initiated")]
        assert all(c["status"] == "initiated" for c in calls(voice_client, voice_tenant, scheduled))
        assert len(mock_provider.initiated) == 2

    def test_crash_during_initiation_is_never_redialled(
        self,
        voice_client,
        voice_tenant,
        scheduled,
        run_worker,
        mock_provider,
        voice_settings,
        session,
    ):
        with pytest.raises(WorkerCrash):
            run_worker(provider=CrashingProvider(mock_provider))
        # Both initiation jobs can share run_after, so either call may be the one that was
        # being initiated when the worker died; follow that call, whichever it is.
        [crashed] = [
            c for c in calls(voice_client, voice_tenant, scheduled) if c["status"] == "initiating"
        ]
        results = run_worker(later=voice_settings.voice_job_lease_seconds + 1)
        assert ("initiate_call", "unknown") in results
        crashed = next(
            c for c in calls(voice_client, voice_tenant, scheduled) if c["id"] == crashed["id"]
        )
        assert (crashed["status"], crashed["outcome"]) == ("unknown", "initiation_interrupted")
        assert not any(c.internal_call_id == crashed["id"] for c in mock_provider.initiated)

    def test_accepted_but_unacknowledged_call_is_recovered_from_the_provider(
        self, voice_client, voice_tenant, scheduled, run_worker, mock_provider, session
    ):
        # Bolna created the call, but the response never reached us.
        mock_provider.script(ALICE_PHONE, MockBehavior.ACCEPTED_THEN_TIMEOUT)
        run_worker()
        alice = calls(voice_client, voice_tenant, scheduled)[0]
        assert alice["status"] == "unknown" and alice["provider_call_id"] is None
        assert sum(c.recipient_phone_number == ALICE_PHONE for c in mock_provider.initiated) == 1

        # Bolna's webhook for that execution arrives; the authoritative fetch carries our
        # correlation ID, so the call is adopted and the decision counts.
        execution = mock_provider.execution_for(alice["id"])
        payload = mock_provider.complete(execution["id"], decision="approve")
        deliver_webhook(voice_client, payload, WEBHOOK_TOKEN)
        assert [r for _, r in run_worker()] == ["vote_recorded"]
        alice = calls(voice_client, voice_tenant, scheduled)[0]
        assert alice["provider_call_id"] == execution["id"]
        assert alice["decision_status"] == "accepted"
        assert count(session, ApprovalVote) == 1

    def test_event_arriving_before_initiation_is_recorded_is_replayed(
        self,
        voice_client,
        voice_tenant,
        scheduled,
        session_factory,
        voice_settings,
        mock_provider,
        run_worker,
        session,
    ):
        # Simulate the race: the provider accepted and already reported progress before
        # phase 3 stored the execution ID. The event is held as unmatched...
        mock_provider.script(ALICE_PHONE, MockBehavior.ACCEPTED_THEN_TIMEOUT)
        run_worker()
        alice = calls(voice_client, voice_tenant, scheduled)[0]
        execution = mock_provider.execution_for(alice["id"])
        payload = mock_provider.set_status(execution["id"], "in-progress")
        deliver_webhook(voice_client, payload, WEBHOOK_TOKEN)
        assert [r for _, r in run_worker()] == ["unmatched"]
        # ...and a later trusted observation adopts the call; the held event is harmless.
        finish = mock_provider.complete(execution["id"], decision="approve")
        deliver_webhook(voice_client, finish, WEBHOOK_TOKEN)
        assert [r for _, r in run_worker()] == ["vote_recorded"]


class TestQueueConcurrency:
    def test_parallel_workers_never_claim_the_same_job(
        self, voice_client, voice_tenant, session_factory, voice_settings, session
    ):
        for i in range(6):
            approver = create_approver(
                voice_client, voice_tenant, f"A{i}", phone_number=f"+1415555011{i}"
            )
            policy = create_policy(voice_client, voice_tenant, [approver["id"]], name=f"P{i}")
            request = create_request(voice_client, voice_tenant, policy["id"])
            schedule_calls(voice_client, voice_tenant, request["id"])

        barrier = threading.Barrier(8)
        claimed: list[UUID] = []
        lock = threading.Lock()

        def worker(n):
            barrier.wait()
            while True:
                with session_factory() as db:
                    job = claim_next(db, voice_settings, f"w{n}")
                if job is None:
                    return
                with lock:
                    claimed.append(job.id)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert len(claimed) == 6 and len(set(claimed)) == 6

    def test_concurrent_approvals_through_voice_decide_once(
        self, voice_client, voice_tenant, scheduled, run_worker, mock_provider, session
    ):
        run_worker()
        for call in calls(voice_client, voice_tenant, scheduled):
            payload = mock_provider.complete(call["provider_call_id"], decision="approve")
            deliver_webhook(voice_client, payload, WEBHOOK_TOKEN)

        barrier = threading.Barrier(2)
        results: list = []

        def drain(n):
            barrier.wait()
            results.extend(run_worker(worker_id=f"w{n}"))

        threads = [threading.Thread(target=drain, args=(n,)) for n in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert sorted(r for _, r in results) == ["vote_recorded", "vote_recorded"]
        assert count(session, ApprovalVote) == 2
        assert count(session, AuditEvent, AuditEvent.event_type == "request.approved") == 1
        status = session.scalar(
            select(ApprovalRequest.status).where(
                ApprovalRequest.id == UUID(scheduled["request_id"])
            )
        )
        assert status == "approved"


class TestReconciliation:
    def test_missed_webhook_is_recovered_by_polling(
        self,
        voice_client,
        voice_tenant,
        scheduled,
        run_worker,
        mock_provider,
        session,
        session_factory,
        voice_settings,
    ):
        run_worker()
        alice = calls(voice_client, voice_tenant, scheduled)[0]
        mock_provider.complete(alice["provider_call_id"], decision="approve")  # no webhook
        later = utcnow() + timedelta(seconds=voice_settings.voice_reconcile_after_seconds + 1)
        with session_factory() as db:
            assert schedule_reconciliations(db, voice_settings, now=later) == 2
            # Scheduling again does not duplicate the active reconciliation jobs.
            assert schedule_reconciliations(db, voice_settings, now=later) == 0
        results = dict((r, t) for t, r in run_worker())
        assert "vote_recorded" in results
        assert count(session, ApprovalVote) == 1

    def test_call_without_final_result_times_out_without_a_vote(
        self,
        voice_client,
        voice_tenant,
        scheduled,
        run_worker,
        session,
        session_factory,
        voice_settings,
    ):
        run_worker()
        long_ago = utcnow() - timedelta(seconds=voice_settings.voice_call_timeout_seconds + 5)
        with session_factory() as db:
            db.execute(update(VoiceCall).values(initiated_at=long_ago, last_event_at=long_ago))
            db.commit()
            schedule_reconciliations(db, voice_settings)
        assert sorted(r for _, r in run_worker()) == ["timed_out", "timed_out"]
        assert {c["status"] for c in calls(voice_client, voice_tenant, scheduled)} == {"timed_out"}
        assert count(session, ApprovalVote) == 0
        assert (
            voice_client.get(
                f"{API}/requests/{scheduled['request_id']}", headers=voice_tenant.agent
            ).json()["status"]
            == "pending"
        )

    def test_timed_out_call_ignores_late_completion(
        self,
        voice_client,
        voice_tenant,
        scheduled,
        run_worker,
        mock_provider,
        session,
        session_factory,
        voice_settings,
    ):
        run_worker()
        alice = calls(voice_client, voice_tenant, scheduled)[0]
        long_ago = utcnow() - timedelta(seconds=voice_settings.voice_call_timeout_seconds + 5)
        with session_factory() as db:
            db.execute(update(VoiceCall).values(initiated_at=long_ago, last_event_at=long_ago))
            db.commit()
            schedule_reconciliations(db, voice_settings)
        run_worker()
        payload = mock_provider.complete(alice["provider_call_id"], decision="approve")
        deliver_webhook(voice_client, payload, WEBHOOK_TOKEN)
        assert [r for _, r in run_worker()] == ["stale"]
        assert count(session, ApprovalVote) == 0


def test_call_binding_is_immutable_in_the_database(scheduled, session):
    call = session.scalars(select(VoiceCall)).first()
    with pytest.raises(Exception, match="binding is immutable"):
        session.execute(
            update(VoiceCall).where(VoiceCall.id == call.id).values(approver_id=UUID(int=0))
        )
    session.rollback()
    session.execute(
        update(VoiceCall).where(VoiceCall.id == call.id).values(provider_call_id="first")
    )
    session.commit()
    with pytest.raises(Exception, match="only be set once"):
        session.execute(
            update(VoiceCall).where(VoiceCall.id == call.id).values(provider_call_id="second")
        )
    session.rollback()


def test_jobs_survive_in_the_database(scheduled, session):
    # Durable queue: scheduling wrote jobs, independent of any running process.
    assert count(session, VoiceJob, VoiceJob.status == "pending") == 2
