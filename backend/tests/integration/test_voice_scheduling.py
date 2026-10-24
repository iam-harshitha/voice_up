import threading

import pytest
from sqlalchemy import func, select

from app.db.models import AuditEvent, VoiceCall, VoiceJob
from app.voice.mock import MockBehavior
from tests.helpers import (
    API,
    create_approver,
    create_policy,
    create_request,
    list_calls,
    schedule_calls,
    vote,
)

pytestmark = pytest.mark.integration

ALICE_PHONE, BOB_PHONE = "+14155550101", "+14155550102"


@pytest.fixture
def voice_request(voice_client, voice_tenant):
    alice = create_approver(voice_client, voice_tenant, "Alice", phone_number=ALICE_PHONE)
    bob = create_approver(voice_client, voice_tenant, "Bob", phone_number=BOB_PHONE)
    policy = create_policy(voice_client, voice_tenant, [alice["id"], bob["id"]])
    request = create_request(
        voice_client, voice_tenant, policy["id"], context={"service": "payments-api"}
    )
    return {"alice": alice, "bob": bob, "policy": policy, "request": request, "id": request["id"]}


def count(session, model, *where):
    return session.scalar(select(func.count()).select_from(model).where(*where))


class TestScheduling:
    def test_one_call_and_job_per_snapshotted_approver(
        self, voice_client, voice_tenant, voice_request, session
    ):
        response = schedule_calls(voice_client, voice_tenant, voice_request["id"])
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["created"] == 2 and body["skipped"] == []
        assert [c["approver_id"] for c in body["calls"]] == [
            voice_request["alice"]["id"],
            voice_request["bob"]["id"],
        ]
        assert all(
            c["status"] == "queued" and c["decision_status"] == "awaiting" for c in body["calls"]
        )
        assert "recipient_phone_number" not in response.text
        assert ALICE_PHONE not in response.text
        assert count(session, VoiceJob) == 2
        assert count(session, AuditEvent, AuditEvent.event_type == "voice.call_scheduled") == 2

    def test_scheduling_is_idempotent(self, voice_client, voice_tenant, voice_request, session):
        first = schedule_calls(voice_client, voice_tenant, voice_request["id"]).json()
        again = schedule_calls(voice_client, voice_tenant, voice_request["id"])
        assert again.status_code == 200 and again.json()["created"] == 0
        assert [c["id"] for c in again.json()["calls"]] == [c["id"] for c in first["calls"]]
        assert count(session, VoiceCall) == 2 and count(session, VoiceJob) == 2

    def test_concurrent_scheduling_creates_calls_once(
        self, voice_settings, session_factory, voice_tenant, voice_request, session
    ):
        from uuid import UUID

        from app.services import voice_service

        barrier = threading.Barrier(4)
        errors: list[Exception] = []

        def go():
            with session_factory() as db:
                barrier.wait()
                try:
                    voice_service.schedule_calls(
                        db,
                        voice_settings,
                        voice_tenant.organization_id,
                        UUID(voice_request["id"]),
                    )
                except Exception as exc:  # collected and asserted below
                    errors.append(exc)

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert errors == []
        assert count(session, VoiceCall) == 2 and count(session, VoiceJob) == 2

    def test_snapshot_not_live_policy_decides_who_is_called(
        self, voice_client, voice_tenant, voice_request
    ):
        carol = create_approver(voice_client, voice_tenant, "Carol", phone_number="+14155550103")
        voice_client.patch(
            f"{API}/policies/{voice_request['policy']['id']}",
            json={"required_approvals": 1, "approver_ids": [carol["id"]]},
            headers=voice_tenant.admin,
        ).raise_for_status()
        body = schedule_calls(voice_client, voice_tenant, voice_request["id"]).json()
        called = {c["approver_id"] for c in body["calls"]}
        assert called == {voice_request["alice"]["id"], voice_request["bob"]["id"]}

    def test_approver_without_phone_or_inactive_is_skipped(self, voice_client, voice_tenant):
        alice = create_approver(voice_client, voice_tenant, "Alice", phone_number=ALICE_PHONE)
        bob = create_approver(voice_client, voice_tenant, "Bob")  # email only
        policy = create_policy(voice_client, voice_tenant, [alice["id"], bob["id"]])
        request = create_request(voice_client, voice_tenant, policy["id"])
        body = schedule_calls(voice_client, voice_tenant, request["id"]).json()
        assert [c["approver_id"] for c in body["calls"]] == [alice["id"]]
        assert body["skipped"] == [{"approver_id": bob["id"], "reason": "no_phone_number"}]

    def test_no_callable_approver_is_400(self, voice_client, voice_tenant):
        bob = create_approver(voice_client, voice_tenant, "Bob")
        policy = create_policy(voice_client, voice_tenant, [bob["id"]])
        request = create_request(voice_client, voice_tenant, policy["id"])
        response = schedule_calls(voice_client, voice_tenant, request["id"])
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "no_callable_approvers"

    def test_decided_request_cannot_be_scheduled(self, voice_client, voice_tenant, voice_request):
        vote(
            voice_client, voice_tenant, voice_request["id"], voice_request["alice"]["id"], "reject"
        )
        response = schedule_calls(voice_client, voice_tenant, voice_request["id"])
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "request_already_decided"

    def test_other_tenant_and_missing_scope(
        self, voice_client, voice_tenant, voice_request, make_tenant
    ):
        other = make_tenant("Globex", voice=["voice:calls:create", "voice:calls:read"])
        assert schedule_calls(voice_client, other, voice_request["id"]).status_code == 404
        assert (
            voice_client.get(
                f"{API}/requests/{voice_request['id']}/voice-calls", headers=other.headers("voice")
            ).status_code
            == 404
        )
        for role in ("agent", "admin", "voter"):
            response = voice_client.post(
                f"{API}/requests/{voice_request['id']}/voice-calls",
                headers=voice_tenant.headers(role),
            )
            assert response.status_code == 403, role

    def test_unknown_body_fields_rejected(self, voice_client, voice_tenant, voice_request):
        response = voice_client.post(
            f"{API}/requests/{voice_request['id']}/voice-calls",
            json={"approver_ids": [voice_request["alice"]["id"]]},
            headers=voice_tenant.headers("voice"),
        )
        assert response.status_code == 422

    def test_voice_disabled_is_503(self, client, make_tenant):
        tenant = make_tenant(voice=["voice:calls:create"])
        alice = create_approver(client, tenant, "Alice", phone_number=ALICE_PHONE)
        policy = create_policy(client, tenant, [alice["id"]])
        request = create_request(client, tenant, policy["id"])
        response = schedule_calls(client, tenant, request["id"])
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "voice_unavailable"


class TestInitiation:
    def test_worker_calls_the_right_people_with_request_context(
        self, voice_client, voice_tenant, voice_request, run_worker, mock_provider
    ):
        schedule_calls(voice_client, voice_tenant, voice_request["id"])
        assert run_worker() == [("initiate_call", "initiated")] * 2
        assert sorted(c.recipient_phone_number for c in mock_provider.initiated) == [
            ALICE_PHONE,
            BOB_PHONE,
        ]
        variables = next(
            c.variables for c in mock_provider.initiated if c.recipient_phone_number == ALICE_PHONE
        )
        assert variables["approver_name"] == "Alice"
        assert variables["requested_action"] == "deploy.production"
        assert variables["request_details"] == "service: payments-api"
        calls = list_calls(voice_client, voice_tenant, voice_request["id"])
        assert all(c["status"] == "initiated" and c["provider_call_id"] for c in calls)
        assert all(c["initiation_attempts"] == 1 for c in calls)
        # Nothing left to do; a second run does not dial again.
        assert run_worker() == []
        assert len(mock_provider.initiated) == 2

    def test_retryable_errors_back_off_then_succeed(
        self, voice_client, voice_tenant, voice_request, run_worker, mock_provider
    ):
        mock_provider.script(ALICE_PHONE, MockBehavior.RETRYABLE_ERROR)
        schedule_calls(voice_client, voice_tenant, voice_request["id"])
        assert sorted(r for _, r in run_worker()) == ["initiated", "retry"]
        alice_call = list_calls(voice_client, voice_tenant, voice_request["id"])[0]
        assert alice_call["status"] == "queued" and alice_call["last_error"] == "connect_error"
        assert run_worker() == []  # backoff: not due yet
        assert run_worker(later=60) == [("initiate_call", "initiated")]
        assert len(mock_provider.initiated) == 3

    def test_retries_are_bounded(
        self, voice_client, voice_tenant, voice_request, run_worker, mock_provider, session
    ):
        mock_provider.script(ALICE_PHONE, *[MockBehavior.RETRYABLE_ERROR] * 10)
        schedule_calls(voice_client, voice_tenant, voice_request["id"])
        results = []
        for _ in range(6):
            results += [r for t, r in run_worker(later=600)]
        alice = list_calls(voice_client, voice_tenant, voice_request["id"])[0]
        assert alice["status"] == "failed" and alice["outcome"] == "initiation_failed"
        dialled = [c for c in mock_provider.initiated if c.recipient_phone_number == ALICE_PHONE]
        assert len(dialled) == 3  # VOICE_JOB_MAX_ATTEMPTS in the test settings

    def test_permanent_error_is_not_retried(
        self, voice_client, voice_tenant, voice_request, run_worker, mock_provider
    ):
        mock_provider.script(ALICE_PHONE, MockBehavior.PERMANENT_ERROR)
        schedule_calls(voice_client, voice_tenant, voice_request["id"])
        run_worker()
        run_worker(later=3600)
        alice = list_calls(voice_client, voice_tenant, voice_request["id"])[0]
        assert (alice["status"], alice["outcome"], alice["last_error"]) == (
            "failed",
            "initiation_rejected",
            "auth_failed",
        )
        assert sum(c.recipient_phone_number == ALICE_PHONE for c in mock_provider.initiated) == 1

    def test_ambiguous_timeout_is_never_redialled(
        self, voice_client, voice_tenant, voice_request, run_worker, mock_provider
    ):
        mock_provider.script(ALICE_PHONE, MockBehavior.AMBIGUOUS_TIMEOUT)
        schedule_calls(voice_client, voice_tenant, voice_request["id"])
        run_worker()
        run_worker(later=3600)
        alice = list_calls(voice_client, voice_tenant, voice_request["id"])[0]
        assert (alice["status"], alice["outcome"]) == ("unknown", "initiation_unknown")
        assert alice["decision_status"] == "no_decision"
        assert sum(c.recipient_phone_number == ALICE_PHONE for c in mock_provider.initiated) == 1

    def test_request_decided_before_dialling_cancels_call(
        self, voice_client, voice_tenant, voice_request, run_worker, mock_provider
    ):
        schedule_calls(voice_client, voice_tenant, voice_request["id"])
        vote(voice_client, voice_tenant, voice_request["id"], voice_request["bob"]["id"], "reject")
        assert [r for _, r in run_worker()] == ["canceled", "canceled"]
        assert mock_provider.initiated == []

    def test_redial_is_explicit_and_bounded(
        self, voice_client, voice_tenant, voice_request, run_worker, mock_provider, voice_settings
    ):
        mock_provider.script(ALICE_PHONE, *[MockBehavior.PERMANENT_ERROR] * 5)
        schedule_calls(voice_client, voice_tenant, voice_request["id"])
        run_worker()
        # A plain re-schedule does not redial.
        assert (
            schedule_calls(voice_client, voice_tenant, voice_request["id"]).json()["created"] == 0
        )
        for generation in range(2, voice_settings.voice_max_call_generations + 1):
            body = schedule_calls(
                voice_client, voice_tenant, voice_request["id"], redial_unanswered=True
            ).json()
            assert body["created"] == 1
            assert body["calls"][0]["generation"] == generation
            run_worker()
        body = schedule_calls(
            voice_client, voice_tenant, voice_request["id"], redial_unanswered=True
        ).json()
        assert body["created"] == 0
        assert body["skipped"] == [
            {"approver_id": voice_request["alice"]["id"], "reason": "max_generations_reached"}
        ]
