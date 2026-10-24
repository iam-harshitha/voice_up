"""Regression tests for defects found in the Chunk 2 security/correctness review.

Each test reproduces a concrete interleaving against real PostgreSQL.
"""

import json
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.config import Settings
from app.db.models import ApprovalVote, VoiceCall
from app.services import voice_webhook_service
from app.services.voice_job_service import run_pending_jobs
from app.services.voice_webhook_service import apply_execution
from app.voice.base import CallRequest, ErrorKind, VoiceProviderError
from app.voice.mock import MockVoiceProvider
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
def one_call(voice_client, voice_tenant):
    alice = create_approver(voice_client, voice_tenant, "Alice", phone_number=ALICE_PHONE)
    policy = create_policy(voice_client, voice_tenant, [alice["id"]])
    request = create_request(voice_client, voice_tenant, policy["id"])
    schedule_calls(voice_client, voice_tenant, request["id"])
    return {"request_id": request["id"], "alice": alice}


class InterleavingProvider:
    """Runs ``during`` while the provider request is in flight, then fails with ``error``."""

    def __init__(self, inner: MockVoiceProvider, during, error: VoiceProviderError):
        self.inner, self.during, self.error = inner, during, error
        self.name, self.agent_id = inner.name, inner.agent_id
        self.dials = 0

    def initiate_call(self, call: CallRequest):
        self.dials += 1
        if self.dials == 1:
            self.during(call)
            raise self.error
        return self.inner.initiate_call(call)

    def get_execution(self, provider_call_id):
        return self.inner.get_execution(provider_call_id)


def test_lease_lost_during_provider_call_does_not_requeue_a_resolved_call(
    voice_client, voice_tenant, one_call, session_factory, voice_settings, mock_provider
):
    """The lease expires mid-request; a second worker marks the call unknown; the first
    worker's request then fails with a retryable error. The call must stay unknown and
    must not be dialled again."""
    holder = {}

    def second_worker_takes_over(_call):
        later = voice_settings.voice_job_lease_seconds + 5
        from datetime import timedelta

        from app.db.base import utcnow

        holder["results"] = run_pending_jobs(
            session_factory,
            provider,
            voice_settings,
            "worker-2",
            now=utcnow() + timedelta(seconds=later),
        )

    provider = InterleavingProvider(
        mock_provider,
        second_worker_takes_over,
        VoiceProviderError(ErrorKind.RETRYABLE, "connect_error"),
    )
    run_pending_jobs(session_factory, provider, voice_settings, "worker-1")
    assert holder["results"] == [("initiate_call", "unknown")]

    call = list_calls(voice_client, voice_tenant, one_call["request_id"])[0]
    assert (call["status"], call["outcome"]) == ("unknown", "initiation_interrupted")
    # Nothing is left queued that could dial the approver a second time.
    for later in (60, 3600):
        from datetime import timedelta

        from app.db.base import utcnow

        run_pending_jobs(
            session_factory,
            provider,
            voice_settings,
            "worker-3",
            now=utcnow() + timedelta(seconds=later),
        )
    assert provider.dials == 1
    assert list_calls(voice_client, voice_tenant, one_call["request_id"])[0]["status"] == (
        "unknown"
    )


def test_ambiguous_result_does_not_overwrite_a_call_the_provider_already_resolved(
    voice_client, voice_tenant, one_call, session_factory, voice_settings, mock_provider
):
    """Bolna accepted the call and it failed fast (busy); the trusted fetch adopted and
    finished it before our request timed out. The timeout must not overwrite that."""

    def provider_resolves_call(call: CallRequest):
        execution_id = mock_provider._create_execution(call)
        mock_provider.set_status(execution_id, "busy")
        with session_factory() as db:
            result = apply_execution(
                db,
                voice_settings,
                mock_provider.name,
                mock_provider.agent_id,
                mock_provider.get_execution(execution_id),
                trusted=True,
            )
        assert result == "status_failed"

    provider = InterleavingProvider(
        mock_provider,
        provider_resolves_call,
        VoiceProviderError(ErrorKind.AMBIGUOUS, "response_timeout"),
    )
    run_pending_jobs(session_factory, provider, voice_settings, "worker-1")
    call = list_calls(voice_client, voice_tenant, one_call["request_id"])[0]
    assert (call["status"], call["outcome"]) == ("failed", "busy")
    assert call["provider_call_id"] is not None


def test_execution_is_never_applied_to_a_call_bound_to_a_different_execution(
    voice_client,
    voice_tenant,
    one_call,
    session_factory,
    voice_settings,
    mock_provider,
    run_worker,
    monkeypatch,
    session,
):
    """If the call gained a different provider ID between the unlocked lookup and the
    row lock, the execution must be treated as unmatched, not applied to that call."""
    run_worker()
    call = list_calls(voice_client, voice_tenant, one_call["request_id"])[0]
    stranger = mock_provider._create_execution(
        CallRequest(call["id"], ALICE_PHONE, {"approval_line_call_id": call["id"]})
    )
    mock_provider.complete(stranger, decision="approve")
    real_find = voice_webhook_service._find_call

    def stale_lookup(db, provider, execution, agent_id, *, trusted):
        found, _ = real_find(db, provider, execution, agent_id, trusted=trusted)
        # Simulate the race: the unlocked read believed the call was adoptable.
        return found or db.get(VoiceCall, uuid.UUID(call["id"])), True

    monkeypatch.setattr(voice_webhook_service, "_find_call", stale_lookup)
    with session_factory() as db:
        result = apply_execution(
            db,
            voice_settings,
            mock_provider.name,
            mock_provider.agent_id,
            mock_provider.get_execution(stranger),
            trusted=True,
        )
    assert result == "unmatched"
    after = list_calls(voice_client, voice_tenant, one_call["request_id"])[0]
    assert after["status"] == "initiated" and after["provider_call_id"] == call["provider_call_id"]
    assert session.scalar(select(func.count()).select_from(ApprovalVote)) == 0


def test_deeply_nested_webhook_json_is_rejected_cleanly(voice_client):
    body = "[" * 100_000 + "]" * 100_000
    response = voice_client.post(
        f"{API}/voice/webhooks/bolna",
        content=body[:4000],  # within the size limit but deeply nested
        headers={
            "Content-Type": "application/json",
            "X-Approval-Line-Webhook-Token": WEBHOOK_TOKEN,
        },
    )
    assert response.status_code == 400
    deep = json.dumps({"id": str(uuid.uuid4())})[:-1] + ', "x": ' + "[" * 2000 + "]" * 2000 + "}"
    response = voice_client.post(
        f"{API}/voice/webhooks/bolna",
        content=deep,
        headers={
            "Content-Type": "application/json",
            "X-Approval-Line-Webhook-Token": WEBHOOK_TOKEN,
        },
    )
    assert response.status_code in (400, 413)


def test_approver_who_already_voted_is_not_called(voice_client, voice_tenant):
    alice = create_approver(voice_client, voice_tenant, "Alice", phone_number=ALICE_PHONE)
    bob = create_approver(voice_client, voice_tenant, "Bob", phone_number=BOB_PHONE)
    policy = create_policy(voice_client, voice_tenant, [alice["id"], bob["id"]])
    request = create_request(voice_client, voice_tenant, policy["id"])
    vote(voice_client, voice_tenant, request["id"], alice["id"], "approve")
    body = schedule_calls(voice_client, voice_tenant, request["id"]).json()
    assert [c["approver_id"] for c in body["calls"]] == [bob["id"]]
    assert body["skipped"] == [{"approver_id": alice["id"], "reason": "already_voted"}]


class CountingProvider:
    def __init__(self, inner):
        self.inner, self.name, self.agent_id = inner, inner.name, inner.agent_id
        self.fetches = 0

    def initiate_call(self, call):
        return self.inner.initiate_call(call)

    def get_execution(self, provider_call_id):
        self.fetches += 1
        return self.inner.get_execution(provider_call_id)


def test_forged_terminal_events_do_not_amplify_into_provider_api_calls(
    voice_client, voice_tenant, one_call, mock_provider, run_worker, session
):
    """A token-bearing flood of terminal events for unknown or already-finished calls
    must not make us call the provider API for each one (Bolna rate-limits per account,
    which would also block real call initiation)."""
    counting = CountingProvider(mock_provider)
    run_worker(provider=counting)
    call = list_calls(voice_client, voice_tenant, one_call["request_id"])[0]
    deliver_webhook(voice_client, mock_provider.complete(call["provider_call_id"]), WEBHOOK_TOKEN)
    run_worker(provider=counting)
    assert counting.fetches == 1  # the genuine completion is verified with the provider

    for i in range(5):
        deliver_webhook(
            voice_client, {"id": str(uuid.uuid4()), "status": "completed"}, WEBHOOK_TOKEN
        )
        deliver_webhook(
            voice_client,
            {"id": call["provider_call_id"], "status": "failed", "updated_at": f"forged-{i}"},
            WEBHOOK_TOKEN,
        )
    run_worker(provider=counting)
    assert counting.fetches == 1
    assert session.scalar(select(func.count()).select_from(ApprovalVote)) == 1


def test_lease_must_comfortably_exceed_provider_timeout():
    db = "postgresql+psycopg://u:p@127.0.0.1:1/approval_line_test"
    with pytest.raises(ValidationError, match="VOICE_JOB_LEASE_SECONDS"):
        Settings(database_url=db, voice_job_lease_seconds=30, voice_http_timeout_seconds=20)
    Settings(database_url=db, voice_job_lease_seconds=120, voice_http_timeout_seconds=10)
