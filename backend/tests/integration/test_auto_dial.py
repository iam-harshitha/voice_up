"""Auto-dial: opt-in per policy. Creating a request under an auto_dial policy schedules the
approvers' calls server-side, in the creation transaction, exactly once."""

import threading
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from app.db.models import ApprovalRequest, AuditEvent, VoiceCall, VoiceJob
from app.db.models.voice import JobType
from app.schemas.request import ApprovalRequestCreate
from app.services import approval_service, voice_service
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
)

pytestmark = pytest.mark.integration

ALICE_PHONE, BOB_PHONE = "+14155550101", "+14155550102"


def count(session, model, *where) -> int:
    session.expire_all()
    return session.scalar(select(func.count()).select_from(model).where(*where))


def initiate_jobs(session) -> int:
    return count(session, VoiceJob, VoiceJob.job_type == JobType.INITIATE_CALL)


def events(session, request_id, event_type) -> list[AuditEvent]:
    session.expire_all()
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.request_id == UUID(request_id), AuditEvent.event_type == event_type)
            .order_by(AuditEvent.sequence)
        )
    )


@pytest.fixture
def tenant(make_tenant):
    # The agent key is the default (requests:create, requests:read): no voice scope.
    return make_tenant(voice=["voice:calls:create", "voice:calls:read"])


def two_approvers(client, tenant, **policy_fields):
    alice = create_approver(client, tenant, "Alice", phone_number=ALICE_PHONE)
    bob = create_approver(client, tenant, "Bob", phone_number=BOB_PHONE)
    return make_policy(client, tenant, [alice["id"], bob["id"]], **policy_fields)


# --- Policy field ----------------------------------------------------------------------


class TestPolicyField:
    def test_defaults_to_false(self, client, make_tenant):
        tenant = make_tenant()
        policy = make_policy(client, tenant, [create_approver(client, tenant)["id"]])
        assert policy["auto_dial"] is False

    def test_create_read_and_update(self, client, make_tenant):
        tenant = make_tenant()
        approver = create_approver(client, tenant)["id"]
        policy = make_policy(client, tenant, [approver], auto_dial=True)
        assert policy["auto_dial"] is True
        url = f"{API}/policies/{policy['id']}"
        assert client.get(url, headers=tenant.admin).json()["auto_dial"] is True

        updated = client.patch(url, json={"auto_dial": False}, headers=tenant.admin).json()
        assert (updated["auto_dial"], updated["version"]) == (False, policy["version"] + 1)
        # Unchanged value: no new version.
        same = client.patch(url, json={"auto_dial": False}, headers=tenant.admin).json()
        assert same["version"] == updated["version"]
        assert client.patch(url, json={"auto_dial": None}, headers=tenant.admin).status_code == 422

    def test_changes_are_audited_on_the_policy(self, client, make_tenant):
        tenant = make_tenant(auditor=["audit:read"])
        policy = make_policy(client, tenant, [create_approver(client, tenant)["id"]])
        client.patch(
            f"{API}/policies/{policy['id']}", json={"auto_dial": True}, headers=tenant.admin
        )
        items = client.get(
            f"{API}/policies/{policy['id']}/audit-events", headers=tenant.headers("auditor")
        ).json()["items"]
        by_type = {item["event_type"]: item["event_data"] for item in items}
        assert by_type["policy.created"]["auto_dial"] is False
        assert "auto_dial" in by_type["policy.updated"]["changed_fields"]
        assert by_type["policy.updated"]["auto_dial"] is True


# --- Default: filing a request never dials ------------------------------------------------


def test_default_policy_never_dials(voice_client, tenant, session):
    policy = two_approvers(voice_client, tenant)
    request = create_request(voice_client, tenant, policy["id"])
    assert list_calls(voice_client, tenant, request["id"]) == []
    assert initiate_jobs(session) == 0
    assert events(session, request["id"], "voice.auto_dial_scheduled") == []
    assert events(session, request["id"], "voice.auto_dial_failed") == []


# --- auto_dial=true -------------------------------------------------------------------------


def test_auto_dial_schedules_calls_with_an_agent_key_only(voice_client, tenant, session):
    policy = two_approvers(voice_client, tenant, auto_dial=True)
    request = create_request(voice_client, tenant, policy["id"])  # agent key, no voice scope
    assert request["status"] == "pending"

    calls = list_calls(voice_client, tenant, request["id"])
    assert [c["status"] for c in calls] == ["queued", "queued"]
    assert initiate_jobs(session) == 2

    scheduled = events(session, request["id"], "voice.call_scheduled")
    assert [e.event_data["reason"] for e in scheduled] == ["auto_dial", "auto_dial"]
    (summary,) = events(session, request["id"], "voice.auto_dial_scheduled")
    assert summary.event_data == {
        "policy_id": policy["id"],
        "policy_version": policy["version"],
        "calls_created": 2,
        "voice_call_ids": [c["id"] for c in calls],
        "skipped": [],
    }
    (created,) = events(session, request["id"], "request.created")
    assert created.event_data["auto_dial"] is True
    assert created.sequence < summary.sequence


def test_auto_dial_end_to_end_with_the_mock_provider(
    voice_client, tenant, mock_provider, run_worker
):
    policy = two_approvers(voice_client, tenant, auto_dial=True)
    request = create_request(voice_client, tenant, policy["id"])
    assert sorted(r for _, r in run_worker()) == ["initiated", "initiated"]
    for call in list_calls(voice_client, tenant, request["id"]):
        payload = mock_provider.complete(call["provider_call_id"], decision="approve")
        assert deliver_webhook(voice_client, payload, WEBHOOK_TOKEN).status_code == 200
        run_worker()
    final = get_request(voice_client, tenant, request["id"])
    assert final["status"] == "approved"
    assert final["evaluation"]["authorized"] is True


def test_manual_schedule_after_auto_dial_creates_nothing(voice_client, tenant, session):
    policy = two_approvers(voice_client, tenant, auto_dial=True)
    request = create_request(voice_client, tenant, policy["id"])
    response = schedule_calls(voice_client, tenant, request["id"])
    assert (response.status_code, response.json()["created"]) == (200, 0)
    assert initiate_jobs(session) == 2


# --- Idempotent replays -----------------------------------------------------------------------


def test_idempotent_replay_does_not_schedule_again(voice_client, tenant, session):
    policy = two_approvers(voice_client, tenant, auto_dial=True)
    body = {"policy_id": policy["id"], "action": "deploy", "summary": "Deploy v2.4"}
    headers = {**tenant.agent, "Idempotency-Key": "auto-dial-replay-1"}
    first = voice_client.post(f"{API}/requests", json=body, headers=headers)
    again = voice_client.post(f"{API}/requests", json=body, headers=headers)
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.headers["Idempotent-Replayed"] == "true"
    assert first.json()["id"] == again.json()["id"]
    assert count(session, VoiceCall) == 2
    assert initiate_jobs(session) == 2
    assert len(events(session, first.json()["id"], "voice.auto_dial_scheduled")) == 1


@pytest.mark.parametrize("attempt", range(3))
def test_concurrent_retries_with_one_key_dial_once(
    voice_client, tenant, voice_settings, session_factory, session, attempt
):
    policy = two_approvers(voice_client, tenant, auto_dial=True)
    data = ApprovalRequestCreate.model_validate(
        {"policy_id": policy["id"], "action": "deploy", "summary": "Deploy v2.4"}
    )
    workers = 6
    barrier = threading.Barrier(workers)
    results: list[object] = []
    lock = threading.Lock()

    def submit():
        with session_factory() as db:
            barrier.wait()
            try:
                request, created = approval_service.submit_request(
                    db,
                    tenant.organization_id,
                    data,
                    idempotency_key=f"auto-dial-race-{attempt}",
                    on_created=lambda s, r, p: voice_service.auto_dial(s, voice_settings, r, p),
                )
                outcome: object = (request.id, created)
            except Exception as exc:  # recorded and asserted below
                outcome = type(exc).__name__
            with lock:
                results.append(outcome)

    threads = [threading.Thread(target=submit) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert all(isinstance(r, tuple) for r in results), results
    assert sum(1 for r in results if r[1]) == 1
    assert count(session, ApprovalRequest) == 1
    # The losers rolled back entirely: one set of calls and one summary event.
    assert count(session, VoiceCall) == 2
    assert initiate_jobs(session) == 2
    assert (
        session.scalar(
            text("SELECT count(*) FROM audit_events WHERE event_type = 'voice.auto_dial_scheduled'")
        )
        == 1
    )


# --- Failures: the request is still created, nothing is dialled, the failure is audited ------


def assert_failed(session, request_id, policy, reason):
    (failed,) = events(session, request_id, "voice.auto_dial_failed")
    assert failed.event_data == {
        "policy_id": policy["id"],
        "policy_version": policy["version"],
        "reason": reason,
    }
    assert events(session, request_id, "voice.auto_dial_scheduled") == []
    assert count(session, VoiceCall) == 0
    assert initiate_jobs(session) == 0


def test_voice_disabled_creates_the_request_and_audits(client, make_tenant, session):
    tenant = make_tenant()
    policy = two_approvers(client, tenant, auto_dial=True)
    request = create_request(client, tenant, policy["id"])
    assert request["status"] == "pending"
    assert request["evaluation"]["authorized"] is False
    assert_failed(session, request["id"], policy, "voice_unavailable")


def test_no_callable_approver_is_audited(voice_client, tenant, session):
    no_phone = create_approver(voice_client, tenant, "Carol")  # e-mail only
    policy = make_policy(voice_client, tenant, [no_phone["id"]], auto_dial=True)
    request = create_request(voice_client, tenant, policy["id"])
    assert request["status"] == "pending"
    assert_failed(session, request["id"], policy, "no_callable_approvers")


def test_database_error_rolls_back_every_call_of_the_attempt(
    voice_client, tenant, session, monkeypatch
):
    policy = two_approvers(voice_client, tenant, auto_dial=True)
    real_create_call = voice_service.create_call
    made: list[object] = []

    def flaky_create_call(*args, **kwargs):
        if made:  # the first call is created, then the second fails
            raise OperationalError("INSERT", {}, Exception("simulated"))
        made.append(real_create_call(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(voice_service, "create_call", flaky_create_call)
    request = create_request(voice_client, tenant, policy["id"])
    assert request["status"] == "pending"
    assert len(made) == 1  # one call existed inside the savepoint before the failure
    assert_failed(session, request["id"], policy, "scheduling_error")
    # The audit trail has no call_scheduled event for the rolled-back call either.
    assert events(session, request["id"], "voice.call_scheduled") == []
