import logging
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.db.models import ApprovalVote, AuditEvent, VoiceProviderEvent
from app.services import approval_service
from app.voice.base import ErrorKind
from tests.conftest import WEBHOOK_TOKEN
from tests.helpers import (
    API,
    create_approver,
    create_policy,
    create_request,
    deliver_webhook,
    list_calls,
    schedule_calls,
    vote,
)

pytestmark = pytest.mark.integration

ALICE_PHONE, BOB_PHONE = "+14155550101", "+14155550102"


@pytest.fixture
def dialled(voice_client, voice_tenant, run_worker, mock_provider):
    """A two-approver request whose calls have been placed with the mock provider."""
    alice = create_approver(voice_client, voice_tenant, "Alice", phone_number=ALICE_PHONE)
    bob = create_approver(voice_client, voice_tenant, "Bob", phone_number=BOB_PHONE)
    policy = create_policy(voice_client, voice_tenant, [alice["id"], bob["id"]])
    request = create_request(voice_client, voice_tenant, policy["id"])
    schedule_calls(voice_client, voice_tenant, request["id"])
    run_worker()
    calls = list_calls(voice_client, voice_tenant, request["id"])
    return {
        "request_id": request["id"],
        "alice": alice,
        "bob": bob,
        "alice_exec": calls[0]["provider_call_id"],
        "bob_exec": calls[1]["provider_call_id"],
    }


def request_status(client, tenant, request_id):
    return client.get(f"{API}/requests/{request_id}", headers=tenant.agent).json()["status"]


def count(session, model, *where):
    session.expire_all()
    return session.scalar(select(func.count()).select_from(model).where(*where))


def finish(client, mock, execution_id, **kwargs):
    """Simulate Bolna finishing the conversation and POSTing the webhook."""
    payload = mock.complete(execution_id, **kwargs)
    return deliver_webhook(client, payload, WEBHOOK_TOKEN)


UNKNOWN_EVENT = {"id": str(uuid.uuid4()), "status": "completed"}


class TestWebhookSecurity:
    @pytest.mark.parametrize(
        "token", [None, "", "wrong", WEBHOOK_TOKEN[:-1] + "y", WEBHOOK_TOKEN + "x"]
    )
    def test_bad_or_missing_token_is_401(self, voice_client, session, token):
        response = deliver_webhook(voice_client, UNKNOWN_EVENT, token)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "webhook_unauthorized"
        assert count(session, VoiceProviderEvent) == 0

    def test_api_key_is_not_a_webhook_credential(self, voice_client, voice_tenant):
        response = deliver_webhook(voice_client, UNKNOWN_EVENT, None, **voice_tenant.voter)
        assert response.status_code == 401

    def test_auth_is_checked_before_content(self, voice_client):
        response = voice_client.post(
            f"{API}/voice/webhooks/bolna",
            content=b"not json",
            headers={"Content-Type": "text/plain"},
        )
        assert response.status_code == 401

    def test_content_type_must_be_json(self, voice_client):
        response = voice_client.post(
            f"{API}/voice/webhooks/bolna",
            content=b"{}",
            headers={"Content-Type": "text/plain", "X-Approval-Line-Webhook-Token": WEBHOOK_TOKEN},
        )
        assert response.status_code == 415

    def test_oversized_body_is_413(self, voice_client, session):
        big = {**UNKNOWN_EVENT, "transcript": "x" * 5000}
        assert deliver_webhook(voice_client, big, WEBHOOK_TOKEN).status_code == 413
        assert count(session, VoiceProviderEvent) == 0

    def test_oversized_body_without_content_length_is_413(self, voice_client):
        def chunks():
            for _ in range(10):
                yield b"x" * 1000

        response = voice_client.post(
            f"{API}/voice/webhooks/bolna",
            content=chunks(),
            headers={
                "Content-Type": "application/json",
                "X-Approval-Line-Webhook-Token": WEBHOOK_TOKEN,
            },
        )
        assert response.status_code == 413

    @pytest.mark.parametrize(
        "body",
        [
            b"{not json",
            b"[]",
            b'"x"',
            b'{"status": "completed"}',
            b'{"id": "../user/me", "status": "completed"}',
            b'{"id": "6b1f8c39-30a5-4d13-a6f3-2b5a9d6f7a10", "status": 5}',
        ],
    )
    def test_malformed_payloads_are_400(self, voice_client, session, body):
        response = voice_client.post(
            f"{API}/voice/webhooks/bolna",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Approval-Line-Webhook-Token": WEBHOOK_TOKEN,
            },
        )
        assert response.status_code == 400
        assert count(session, VoiceProviderEvent) == 0

    def test_unsupported_status_is_acknowledged_and_ignored(self, voice_client, session):
        response = deliver_webhook(
            voice_client, {"id": str(uuid.uuid4()), "status": "teleported"}, WEBHOOK_TOKEN
        )
        assert response.status_code == 200 and response.json() == {"status": "ignored"}
        assert count(session, VoiceProviderEvent) == 0

    def test_webhook_is_absent_when_voice_is_disabled(self, client):
        response = deliver_webhook(client, UNKNOWN_EVENT, WEBHOOK_TOKEN)
        assert response.status_code == 404

    def test_store_failure_is_503_so_the_provider_retries(self, voice_client, monkeypatch):
        from app.api.v1.endpoints import voice_webhooks

        def broken(*args, **kwargs):
            raise OperationalError("INSERT", {}, Exception("db down"))

        monkeypatch.setattr(voice_webhooks, "ingest_webhook", broken)
        response = deliver_webhook(voice_client, UNKNOWN_EVENT, WEBHOOK_TOKEN)
        assert response.status_code == 503

    def test_logs_contain_no_secrets_or_payload(self, voice_client, dialled, mock_provider, caplog):
        caplog.set_level(logging.DEBUG)
        payload = mock_provider.complete(dialled["alice_exec"])
        payload["transcript"] = "SECRET-TRANSCRIPT-CONTENT"
        deliver_webhook(voice_client, payload, WEBHOOK_TOKEN)
        deliver_webhook(voice_client, payload, "wrong-token-value")
        logged = caplog.text + " ".join(str(r.__dict__) for r in caplog.records)
        for secret in (WEBHOOK_TOKEN, "wrong-token-value", "SECRET-TRANSCRIPT", ALICE_PHONE):
            assert secret not in logged


class TestDeliverySemantics:
    def test_identical_redelivery_is_deduplicated(
        self, voice_client, dialled, mock_provider, session
    ):
        payload = mock_provider.complete(dialled["alice_exec"])
        first = deliver_webhook(voice_client, payload, WEBHOOK_TOKEN)
        again = deliver_webhook(voice_client, payload, WEBHOOK_TOKEN)
        assert (first.json(), again.json()) == ({"status": "accepted"}, {"status": "duplicate"})
        assert count(session, VoiceProviderEvent) == 1

    def test_distinct_events_for_one_call_progress_the_lifecycle(
        self, voice_client, voice_tenant, dialled, mock_provider, run_worker
    ):
        for status in ("ringing", "in-progress", "call-disconnected"):
            payload = mock_provider.set_status(dialled["alice_exec"], status)
            payload["updated_at"] = status  # distinct deliveries
            assert deliver_webhook(voice_client, payload, WEBHOOK_TOKEN).json() == {
                "status": "accepted"
            }
        run_worker()
        alice = list_calls(voice_client, voice_tenant, dialled["request_id"])[0]
        assert alice["status"] == "processing" and alice["answered_at"]
        assert alice["decision_status"] == "awaiting"

    def test_out_of_order_events_never_regress(
        self, voice_client, voice_tenant, dialled, mock_provider, run_worker, session
    ):
        # Two deliveries received together may be processed in either order; the outcome
        # must not depend on it.
        finish(voice_client, mock_provider, dialled["alice_exec"])
        late = {
            **mock_provider.payload(dialled["alice_exec"]),
            "status": "in-progress",
            "updated_at": "earlier",
        }
        deliver_webhook(voice_client, late, WEBHOOK_TOKEN)
        results = [r for _, r in run_worker()]
        assert "vote_recorded" in results
        alice = list_calls(voice_client, voice_tenant, dialled["request_id"])[0]
        assert (alice["status"], alice["decision_status"]) == ("completed", "accepted")
        assert count(session, ApprovalVote) == 1

        # A late lifecycle event after the call finished is always stale.
        later = {**late, "updated_at": "even-later"}
        deliver_webhook(voice_client, later, WEBHOOK_TOKEN)
        assert [r for _, r in run_worker()] == ["stale"]
        alice = list_calls(voice_client, voice_tenant, dialled["request_id"])[0]
        assert alice["status"] == "completed"

    def test_unknown_execution_creates_nothing(
        self, voice_client, voice_tenant, run_worker, session
    ):
        payload = {"id": str(uuid.uuid4()), "status": "completed"}
        assert deliver_webhook(voice_client, payload, WEBHOOK_TOKEN).status_code == 200
        # No call matches and none awaits adoption: held as unmatched without spending a
        # provider API call (see test_voice_review_regressions for the fetch counting).
        assert [r for _, r in run_worker()] == ["unmatched"]
        assert count(session, ApprovalVote) == 0


class TestDecisions:
    def test_explicit_approval_records_exactly_one_vote(
        self, voice_client, voice_tenant, dialled, mock_provider, run_worker, session
    ):
        finish(voice_client, mock_provider, dialled["alice_exec"], decision="approve")
        assert [r for _, r in run_worker()] == ["vote_recorded"]
        assert request_status(voice_client, voice_tenant, dialled["request_id"]) == "pending"
        alice = list_calls(voice_client, voice_tenant, dialled["request_id"])[0]
        assert (alice["decision_status"], alice["decision_candidate"], alice["outcome"]) == (
            "accepted",
            "approve",
            "vote_recorded",
        )
        votes = session.scalars(select(ApprovalVote)).all()
        assert [(str(v.approver_id), v.vote) for v in votes] == [
            (dialled["alice"]["id"], "approve")
        ]
        assert alice["vote_id"] == str(votes[0].id)
        recorded = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "vote.recorded")
        ).one()
        assert recorded.event_data["source"] == "voice"
        assert recorded.event_data["voice_call_id"] == alice["id"]

    def test_explicit_rejection_rejects_the_request(
        self, voice_client, voice_tenant, dialled, mock_provider, run_worker
    ):
        finish(voice_client, mock_provider, dialled["bob_exec"], decision="reject")
        run_worker()
        assert request_status(voice_client, voice_tenant, dialled["request_id"]) == "rejected"

    @pytest.mark.parametrize(
        ("kwargs", "outcome"),
        [
            ({"decision": "uncertain"}, "uncertain"),
            ({"decision": "needs_clarification"}, "needs_clarification"),
            ({"decision": None}, "decision_missing"),
            ({"decision": "approve", "confidence": 0.4}, "low_confidence"),
            ({"decision": "approve", "identity": "no"}, "identity_not_confirmed"),
            ({"decision": "approve", "identity": None}, "identity_not_confirmed"),
            ({"decision": "approve", "voicemail": True}, "voicemail"),
            ({"extracted_data": {"Approval": "garbage"}}, "extraction_malformed"),
            ({"extracted_data": {}}, "identity_not_confirmed"),
        ],
    )
    def test_no_valid_decision_means_no_vote(
        self,
        voice_client,
        voice_tenant,
        dialled,
        mock_provider,
        run_worker,
        session,
        kwargs,
        outcome,
    ):
        finish(voice_client, mock_provider, dialled["alice_exec"], **kwargs)
        run_worker()
        alice = list_calls(voice_client, voice_tenant, dialled["request_id"])[0]
        assert (alice["status"], alice["decision_status"], alice["outcome"]) == (
            "completed",
            "no_decision",
            outcome,
        )
        assert count(session, ApprovalVote) == 0
        assert count(session, AuditEvent, AuditEvent.event_type == "voice.decision_rejected") == 1
        assert request_status(voice_client, voice_tenant, dialled["request_id"]) == "pending"

    @pytest.mark.parametrize("status", ["no-answer", "busy", "failed", "balance-low"])
    def test_failed_calls_never_vote(
        self, voice_client, voice_tenant, dialled, mock_provider, run_worker, session, status
    ):
        payload = mock_provider.set_status(dialled["alice_exec"], status)
        deliver_webhook(voice_client, payload, WEBHOOK_TOKEN)
        run_worker()
        alice = list_calls(voice_client, voice_tenant, dialled["request_id"])[0]
        assert (alice["status"], alice["outcome"]) == ("failed", status)
        assert count(session, ApprovalVote) == 0

    def test_forged_webhook_body_cannot_choose_the_decision(
        self, voice_client, voice_tenant, dialled, mock_provider, run_worker, session
    ):
        # The provider's record says "reject"; the delivered body claims "approve".
        real = mock_provider.complete(dialled["alice_exec"], decision="reject")
        forged = {
            **real,
            "extracted_data": {
                "Approval": {
                    "Decision": {"objective": "approve", "confidence": 1.0},
                    "Identity Confirmed": {"objective": "yes", "confidence": 1.0},
                }
            },
        }
        deliver_webhook(voice_client, forged, WEBHOOK_TOKEN)
        run_worker()
        assert [v.vote for v in session.scalars(select(ApprovalVote))] == ["reject"]

    def test_forged_completion_for_live_call_records_nothing(
        self, voice_client, voice_tenant, dialled, mock_provider, run_worker, session
    ):
        live = mock_provider.set_status(dialled["alice_exec"], "in-progress")
        deliver_webhook(voice_client, {**live, "status": "completed"}, WEBHOOK_TOKEN)
        run_worker()
        alice = list_calls(voice_client, voice_tenant, dialled["request_id"])[0]
        assert alice["status"] == "in_progress" and count(session, ApprovalVote) == 0

    def test_execution_bound_to_another_call_is_refused(
        self, voice_client, voice_tenant, dialled, mock_provider, run_worker, session
    ):
        # Bob's execution claims (via its echoed variables) to be Alice's call.
        payload = mock_provider.payload(dialled["bob_exec"])
        alice_call = list_calls(voice_client, voice_tenant, dialled["request_id"])[0]
        payload["context_details"]["recipient_data"]["approval_line_call_id"] = alice_call["id"]
        mock_provider._store.put(dialled["bob_exec"], payload)
        finish(voice_client, mock_provider, dialled["bob_exec"])
        run_worker()
        bob = list_calls(voice_client, voice_tenant, dialled["request_id"])[1]
        assert bob["outcome"] == "correlation_mismatch"
        assert count(session, ApprovalVote) == 0

    def test_execution_from_another_agent_is_refused(
        self, voice_client, voice_tenant, dialled, mock_provider, run_worker, session
    ):
        payload = mock_provider.payload(dialled["alice_exec"])
        payload["agent_id"] = str(uuid.uuid4())
        mock_provider._store.put(dialled["alice_exec"], payload)
        finish(voice_client, mock_provider, dialled["alice_exec"])
        run_worker()
        assert (
            list_calls(voice_client, voice_tenant, dialled["request_id"])[0]["outcome"]
            == "correlation_mismatch"
        )
        assert count(session, ApprovalVote) == 0

    def test_late_approval_after_rejection_is_refused(
        self, voice_client, voice_tenant, dialled, mock_provider, run_worker, session
    ):
        finish(voice_client, mock_provider, dialled["bob_exec"], decision="reject")
        run_worker()
        finish(voice_client, mock_provider, dialled["alice_exec"], decision="approve")
        run_worker()
        alice = list_calls(voice_client, voice_tenant, dialled["request_id"])[0]
        assert (alice["decision_status"], alice["outcome"]) == (
            "refused",
            "request_already_decided",
        )
        assert request_status(voice_client, voice_tenant, dialled["request_id"]) == "rejected"
        assert [v.vote for v in session.scalars(select(ApprovalVote))] == ["reject"]

    def test_conflicting_decision_from_same_approver_is_refused(
        self, voice_client, voice_tenant, dialled, mock_provider, run_worker, session
    ):
        # Alice approved through the trusted vote endpoint; her call then says reject.
        vote(voice_client, voice_tenant, dialled["request_id"], dialled["alice"]["id"], "approve")
        finish(voice_client, mock_provider, dialled["alice_exec"], decision="reject")
        run_worker()
        alice = list_calls(voice_client, voice_tenant, dialled["request_id"])[0]
        assert (alice["decision_status"], alice["outcome"]) == ("refused", "vote_conflict")
        assert request_status(voice_client, voice_tenant, dialled["request_id"]) == "pending"

    def test_redelivery_after_processing_adds_nothing(
        self, voice_client, dialled, mock_provider, run_worker, session
    ):
        payload = mock_provider.complete(dialled["alice_exec"])
        deliver_webhook(voice_client, payload, WEBHOOK_TOKEN)
        run_worker()
        before = count(session, AuditEvent)
        # Same status, new updated_at: a distinct delivery that changes nothing.
        deliver_webhook(voice_client, {**payload, "updated_at": "later"}, WEBHOOK_TOKEN)
        assert [r for _, r in run_worker()] == ["duplicate"]
        assert count(session, ApprovalVote) == 1
        assert count(session, AuditEvent) == before + 1  # one voice.event_ignored
        assert count(session, AuditEvent, AuditEvent.event_type == "vote.recorded") == 1


class TestTransactions:
    def test_failure_while_applying_rolls_back_and_retries(
        self,
        voice_client,
        voice_tenant,
        dialled,
        mock_provider,
        run_worker,
        session,
        monkeypatch,
    ):
        real = approval_service.record_vote

        def broken(*args, **kwargs):
            raise RuntimeError("injected database failure")

        monkeypatch.setattr(approval_service, "record_vote", broken)
        finish(voice_client, mock_provider, dialled["alice_exec"])
        assert [r for _, r in run_worker()] == ["internal_error"]
        alice = list_calls(voice_client, voice_tenant, dialled["request_id"])[0]
        # Nothing from the failed attempt persisted: call not completed, event unprocessed.
        assert alice["status"] == "initiated" and alice["decision_status"] == "awaiting"
        assert count(session, VoiceProviderEvent, VoiceProviderEvent.status == "received") == 1
        assert count(session, ApprovalVote) == 0

        monkeypatch.setattr(approval_service, "record_vote", real)
        assert [r for _, r in run_worker(later=60)] == ["vote_recorded"]
        assert count(session, ApprovalVote) == 1

    def test_transient_fetch_failure_is_retried(
        self, voice_client, dialled, mock_provider, run_worker, session
    ):
        mock_provider.fail_next_fetch(dialled["alice_exec"], ErrorKind.RETRYABLE)
        finish(voice_client, mock_provider, dialled["alice_exec"])
        assert [r for _, r in run_worker()] == ["retry"]
        assert count(session, ApprovalVote) == 0
        assert [r for _, r in run_worker(later=60)] == ["vote_recorded"]
