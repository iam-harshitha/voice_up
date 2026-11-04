"""Text (Telegram) approvals end to end, in mock mode: no network, real checks.

Requests are created through the real API with the agent key, prompts are sent and taps
handled by the real text worker loop, and taps arrive as Telegram-shaped updates parsed
by the real adapter. Telegram IDs and phone numbers are fictional.
"""

import threading
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from app.cli import main as cli_main
from app.db.base import utcnow
from app.db.models import (
    ApprovalRequest,
    ApprovalVote,
    AuditEvent,
    EscalationStep,
    TextPrompt,
    VoiceCall,
    VoiceJob,
)
from app.db.models.text import PromptStatus
from app.db.session import transaction
from app.services import text_service, voice_service
from app.services.voice_job_service import run_pending_jobs
from app.text import message as msg
from app.text import tokens
from app.text.base import CallbackTap, ErrorKind, TextProviderError
from app.workers import text_worker as text_worker_module
from tests.helpers import (
    API,
    age_request,
    create_approver,
    get_request,
    make_policy,
    post_policy,
    schedule_calls,
    vote,
)

pytestmark = pytest.mark.integration

ALICE_TG, BOB_TG, CAROL_TG, MALLORY_TG = "7000001", "7000002", "7000003", "7000666"
NOT_ACCEPTED = msg.NOT_ACCEPTED


# --- Helpers -------------------------------------------------------------------------------


@pytest.fixture
def tenant(make_tenant):
    return make_tenant(voice=["voice:calls:create", "voice:calls:read"])


def paired(client, tenant, worker, name: str, phone: str, telegram_id: str) -> dict:
    approver = create_approver(client, tenant, name, phone_number=phone)
    url = client.post(
        f"{API}/approvers/{approver['id']}/channel-links", json={}, headers=tenant.admin
    ).json()["pairing_url"]
    worker.provider.send_text(f"/start {url.split('?start=')[1]}", sender_id=telegram_id)
    worker.run()
    return approver


def text_policy(client, tenant, approver_ids: list[str], **fields) -> dict:
    return make_policy(
        client, tenant, approver_ids, auto_dial=True, approval_channel="text", **fields
    )


def create(client, tenant, policy_id: str, **fields) -> dict:
    body = {"policy_id": policy_id, "action": "deploy.production", "summary": "Deploy 1.4.2"}
    response = client.post(f"{API}/requests", json={**body, **fields}, headers=tenant.agent)
    assert response.status_code == 201, response.text
    return response.json()


def prompt_of(session, request_id: str, approver_id: str | None = None) -> TextPrompt:
    session.expire_all()
    query = select(TextPrompt).where(TextPrompt.request_id == UUID(request_id))
    if approver_id is not None:
        query = query.where(TextPrompt.approver_id == UUID(approver_id))
    return session.scalars(query.order_by(TextPrompt.created_at.desc())).first()


def count(session, model, *where) -> int:
    session.expire_all()
    return session.scalar(select(func.count()).select_from(model).where(*where))


def events(session, request_id: str, event_type: str) -> list[dict]:
    session.expire_all()
    return [
        e.event_data
        for e in session.scalars(
            select(AuditEvent)
            .where(AuditEvent.request_id == UUID(request_id), AuditEvent.event_type == event_type)
            .order_by(AuditEvent.sequence)
        )
    ]


def tap(worker, prompt: TextPrompt, action: str, *, sender=None, chat=None, data=None) -> str:
    """Tap a button and return the bot's answer to that tap."""
    provider = worker.provider
    message_id = prompt.provider_message_id
    stored = provider.message(message_id)
    provider.tap(
        data or provider.button_data(message_id, action),
        sender_id=sender or ALICE_TG,
        chat_id=chat or stored["chat_id"],
        message_id=message_id,
    )
    worker.run()
    return provider.answers[-1][1]


@pytest.fixture
def world(text_client, tenant, text_worker, session):
    """Alice (paired) on a text policy with auto-dial on; one request, prompt delivered."""
    alice = paired(text_client, tenant, text_worker, "Alice", "+14155550101", ALICE_TG)
    policy = text_policy(text_client, tenant, [alice["id"]])
    request = create(text_client, tenant, policy["id"], context={"service": "payments-api"})
    text_worker.run()
    prompt = prompt_of(session, request["id"])
    assert prompt.status == PromptStatus.SENT
    return {"alice": alice, "policy": policy, "request": request, "prompt": prompt}


def no_vote(session, request_id: str) -> bool:
    return count(session, ApprovalVote, ApprovalVote.request_id == UUID(request_id)) == 0


def rejected_reasons(session, request_id: str) -> list[str]:
    return [e["reason"] for e in events(session, request_id, "text.decision_rejected")]


# --- The happy paths -------------------------------------------------------------------------


def test_a_text_policy_never_dials_even_with_auto_dial_on(world, session):
    request_id = world["request"]["id"]
    assert count(session, VoiceCall, VoiceCall.request_id == UUID(request_id)) == 0
    assert count(session, VoiceJob, VoiceJob.job_type == "initiate_call") == 0
    assert events(session, request_id, "voice.auto_dial_scheduled") == []
    (created,) = events(session, request_id, "request.created")
    assert created["approval_channel"] == "text" and created["auto_dial"] is True
    session.expire_all()
    snapshot = session.get(ApprovalRequest, UUID(request_id)).policy_snapshot
    assert snapshot["approval_channel"] == "text"


def test_the_message_shows_the_sanitised_context_and_two_buttons(
    text_client, tenant, text_worker, session
):
    alice = paired(text_client, tenant, text_worker, "Alice", "+14155550101", ALICE_TG)
    policy = text_policy(text_client, tenant, [alice["id"]])
    request = create(
        text_client,
        tenant,
        policy["id"],
        context={"service": "payments-api", "risk": "high", "api_key": "sk-do-not-show"},
    )
    text_worker.run()
    prompt = prompt_of(session, request["id"])
    stored = text_worker.provider.message(prompt.provider_message_id)
    assert stored["chat_id"] == ALICE_TG
    assert f"Approval request {request['id'][:8].upper()}" in stored["text"]
    assert "service: payments-api" in stored["text"] and "risk: high" in stored["text"]
    assert "sk-do-not-show" not in stored["text"]  # secret-looking keys are dropped
    assert [b["label"] for b in stored["buttons"]] == ["Approve", "Reject"]
    assert all(len(b["data"].encode()) <= 64 for b in stored["buttons"])
    nonce = tokens.verify(text_service._secret(text_worker.settings), stored["buttons"][0]["data"])[
        1
    ]
    assert prompt.token_hash == tokens.sha256_hex(nonce)  # only the hash is stored
    (sent,) = events(session, request["id"], "text.prompt_sent")
    assert sent["text_prompt_id"] == str(prompt.id) and sent["reason"] == "auto_dial"


def test_approve_needs_a_confirmation_tap_then_approves(
    world, text_client, tenant, text_worker, session
):
    request_id = world["request"]["id"]
    assert tap(text_worker, world["prompt"], "a") == "Tap 'Confirm approve' to record it."
    prompt = prompt_of(session, request_id)
    assert prompt.status == PromptStatus.CONFIRMING and no_vote(session, request_id)
    shown = text_worker.provider.message(prompt.provider_message_id)
    assert [b["label"] for b in shown["buttons"]] == ["Confirm approve", "Cancel"]

    assert tap(text_worker, prompt, "c") == "Approved: recorded."
    status = get_request(text_client, tenant, request_id)  # what the guard/MCP read
    assert status["status"] == "approved" and status["evaluation"]["authorized"] is True
    prompt = prompt_of(session, request_id)
    assert (prompt.status, prompt.decision, prompt.outcome) == (
        "answered",
        "approve",
        "vote_recorded",
    )
    (recorded,) = events(session, request_id, "vote.recorded")
    assert recorded["source"] == "telegram" and recorded["text_prompt_id"] == str(prompt.id)
    (accepted,) = events(session, request_id, "text.decision_accepted")
    assert accepted["vote"] == "approve" and accepted["request_status"] == "approved"
    final = text_worker.provider.message(prompt.provider_message_id)
    assert final["buttons"] == [] and final["text"].endswith(msg.OUTCOME_NOTES["approve"])


def test_reject_records_a_rejection(world, text_client, tenant, text_worker, session):
    assert tap(text_worker, world["prompt"], "r") == "Rejected: recorded."
    assert get_request(text_client, tenant, world["request"]["id"])["status"] == "rejected"
    assert events(session, world["request"]["id"], "text.decision_accepted")[0]["vote"] == "reject"


def test_cancel_returns_to_the_buttons_with_fresh_tokens(world, text_worker, session):
    request_id = world["request"]["id"]
    old_approve = text_worker.provider.button_data(world["prompt"].provider_message_id, "a")
    tap(text_worker, world["prompt"], "a")
    assert (
        tap(text_worker, prompt_of(session, request_id), "x") == "Cancelled. Nothing was recorded."
    )
    prompt = prompt_of(session, request_id)
    assert prompt.status == PromptStatus.SENT and no_vote(session, request_id)
    assert tap(text_worker, prompt, "a", data=old_approve) == NOT_ACCEPTED  # old token is dead
    assert tap(text_worker, prompt, "r") == "Rejected: recorded."


# --- Security: every refused tap records nothing ------------------------------------------------


def test_unknown_sender_is_refused_and_the_real_approver_can_still_answer(
    world, text_worker, session
):
    request_id = world["request"]["id"]
    assert tap(text_worker, world["prompt"], "r", sender=MALLORY_TG) == NOT_ACCEPTED
    assert no_vote(session, request_id) and prompt_of(session, request_id).status == "sent"
    assert rejected_reasons(session, request_id) == ["sender_mismatch"]
    assert MALLORY_TG not in str(events(session, request_id, "text.decision_rejected"))
    assert tap(text_worker, world["prompt"], "r") == "Rejected: recorded."


def test_a_replayed_token_is_refused(world, text_worker, session):
    request_id = world["request"]["id"]
    data = text_worker.provider.button_data(world["prompt"].provider_message_id, "r")
    assert tap(text_worker, world["prompt"], "r", data=data) == "Rejected: recorded."
    assert tap(text_worker, world["prompt"], "r", data=data) == NOT_ACCEPTED
    assert count(session, ApprovalVote, ApprovalVote.request_id == UUID(request_id)) == 1
    assert rejected_reasons(session, request_id) == ["token_used"]


def test_an_expired_token_is_refused_and_closes_the_prompt(
    world, text_worker, session, session_factory
):
    request_id = world["request"]["id"]
    with session_factory() as db:
        db.execute(
            update(TextPrompt)
            .where(TextPrompt.id == world["prompt"].id)
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        db.commit()
    assert tap(text_worker, world["prompt"], "r") == NOT_ACCEPTED
    assert no_vote(session, request_id)
    assert prompt_of(session, request_id).status == PromptStatus.CLOSED
    assert rejected_reasons(session, request_id) == ["token_expired"]
    assert text_worker.provider.message(world["prompt"].provider_message_id)["buttons"] == []


def test_a_tampered_signature_is_refused(world, text_worker, session):
    data = text_worker.provider.button_data(world["prompt"].provider_message_id, "r")
    tampered = data[:-1] + ("A" if data[-1] != "A" else "B")
    assert tap(text_worker, world["prompt"], "r", data=tampered) == NOT_ACCEPTED
    assert no_vote(session, world["request"]["id"])
    # The nonce is real, so the attempt is attributed to its request's audit trail.
    assert rejected_reasons(session, world["request"]["id"]) == ["bad_signature"]


def test_a_swapped_action_is_refused(world, text_worker, session):
    reject = text_worker.provider.button_data(world["prompt"].provider_message_id, "r")
    for swapped in (reject.replace(":r:", ":c:", 1), reject.replace(":r:", ":a:", 1)):
        assert tap(text_worker, world["prompt"], "r", data=swapped) == NOT_ACCEPTED
    assert no_vote(session, world["request"]["id"])
    assert prompt_of(session, world["request"]["id"]).status == PromptStatus.SENT
    assert rejected_reasons(session, world["request"]["id"]) == ["bad_signature"] * 2


def test_a_validly_signed_but_unknown_token_is_refused(world, text_worker, session):
    forged = tokens.sign(text_service._secret(text_worker.settings), "c", tokens.new_nonce())
    assert tap(text_worker, world["prompt"], "c", data=forged) == NOT_ACCEPTED
    assert no_vote(session, world["request"]["id"])


def test_a_request_already_closed_refuses_the_tap(world, text_client, tenant, text_worker, session):
    request_id = world["request"]["id"]
    assert vote(text_client, tenant, request_id, world["alice"]["id"], "reject").status_code == 201
    assert tap(text_worker, world["prompt"], "a") == NOT_ACCEPTED
    assert rejected_reasons(session, request_id) == ["request_closed"]
    assert prompt_of(session, request_id).status == PromptStatus.CLOSED
    shown = text_worker.provider.message(world["prompt"].provider_message_id)
    assert shown["buttons"] == [] and shown["text"].endswith(msg.OUTCOME_NOTES["closed"])


def test_a_conflicting_second_vote_is_refused(text_client, tenant, text_worker, session):
    alice = paired(text_client, tenant, text_worker, "Alice", "+14155550101", ALICE_TG)
    bob = create_approver(text_client, tenant, "Bob", phone_number="+14155550102")
    policy = text_policy(text_client, tenant, [alice["id"], bob["id"]])
    request = create(text_client, tenant, policy["id"])
    text_worker.run()
    prompt = prompt_of(session, request["id"], alice["id"])
    assert vote(text_client, tenant, request["id"], alice["id"], "approve").status_code == 201
    assert tap(text_worker, prompt, "r") == NOT_ACCEPTED
    assert rejected_reasons(session, request["id"]) == ["vote_conflict"]
    assert count(session, ApprovalVote, ApprovalVote.request_id == UUID(request["id"])) == 1
    assert get_request(text_client, tenant, request["id"])["status"] == "pending"
    assert prompt_of(session, request["id"], alice["id"]).outcome == "vote_conflict"


def test_a_free_text_reply_is_never_counted(world, text_worker, session):
    for text in ("approve", "yes", "/approve", "I approve"):
        text_worker.provider.send_text(text, sender_id=ALICE_TG)
    text_worker.run()
    replies = [m["text"] for m in text_worker.provider.outbox() if m["kind"] == "message"]
    assert replies[-4:] == [msg.FREE_TEXT_REPLY] * 4
    assert no_vote(session, world["request"]["id"])
    assert prompt_of(session, world["request"]["id"]).status == PromptStatus.SENT


def test_a_tap_from_the_wrong_chat_is_refused(world, text_worker, session):
    assert tap(text_worker, world["prompt"], "r", chat="-100777") == NOT_ACCEPTED
    assert no_vote(session, world["request"]["id"])
    assert rejected_reasons(session, world["request"]["id"]) == ["wrong_chat"]


def test_a_revoked_link_stops_open_prompts(world, text_client, tenant, text_worker, session):
    text_client.delete(
        f"{API}/approvers/{world['alice']['id']}/channel-links", headers=tenant.admin
    )
    assert tap(text_worker, world["prompt"], "r") == NOT_ACCEPTED
    assert no_vote(session, world["request"]["id"])
    assert rejected_reasons(session, world["request"]["id"]) == ["link_revoked"]


def test_concurrent_confirm_taps_record_one_vote(world, text_worker, session, session_factory):
    tap(text_worker, world["prompt"], "a")
    prompt = prompt_of(session, world["request"]["id"])
    data = text_worker.provider.button_data(prompt.provider_message_id, "c")
    results = []

    def confirm(n: int) -> None:
        tap_update = CallbackTap(n, f"cb{n}", ALICE_TG, ALICE_TG, prompt.provider_message_id, data)
        with session_factory() as db:
            results.append(text_service.handle_tap(db, text_worker.settings, tap_update).reason)

    threads = [threading.Thread(target=confirm, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["token_used"] * 3 + ["vote_recorded"]
    assert (
        count(session, ApprovalVote, ApprovalVote.request_id == UUID(world["request"]["id"])) == 1
    )


# --- Delivery, configuration and the other paths ------------------------------------------------


def test_an_approver_without_a_verified_link_gets_no_prompt_and_no_call(
    text_client, tenant, session
):
    bob = create_approver(text_client, tenant, "Bob", phone_number="+14155550102")
    request = create(text_client, tenant, text_policy(text_client, tenant, [bob["id"]])["id"])
    assert count(session, TextPrompt) == 0 and count(session, VoiceCall) == 0
    (failed,) = events(session, request["id"], "text.prompt_failed")
    assert failed == {"approver_id": bob["id"], "reason": "no_verified_link", "stage": "auto_dial"}


def test_text_disabled_means_no_prompt_and_still_no_call(voice_client, tenant, session):
    alice = create_approver(voice_client, tenant, "Alice", phone_number="+14155550101")
    request = create(voice_client, tenant, text_policy(voice_client, tenant, [alice["id"]])["id"])
    assert count(session, VoiceCall) == 0 and count(session, TextPrompt) == 0
    assert events(session, request["id"], "text.prompt_failed")[0]["reason"] == "text_unavailable"
    response = schedule_calls(voice_client, tenant, request["id"])
    assert response.status_code == 400 and response.json()["error"]["code"] == "text_channel"


def test_retryable_send_errors_retry_then_fail_and_are_audited(
    text_client, tenant, text_worker, session, session_factory
):
    alice = paired(text_client, tenant, text_worker, "Alice", "+14155550101", ALICE_TG)
    request = create(text_client, tenant, text_policy(text_client, tenant, [alice["id"]])["id"])
    provider = text_worker.provider
    provider.fail_next = [TextProviderError(ErrorKind.RETRYABLE, "connect_error")] * 3
    later = utcnow()
    for _ in range(3):
        text_service.send_due_prompts(session_factory, provider, text_worker.settings, now=later)
        later += timedelta(minutes=5)
    prompt = prompt_of(session, request["id"])
    assert (prompt.status, prompt.attempts, prompt.token_hash) == ("failed", 3, None)
    (failed,) = events(session, request["id"], "text.prompt_failed")
    assert failed["reason"] == "connect_error" and failed["attempts"] == 3


def test_a_permanent_send_error_fails_at_once(text_client, tenant, text_worker, session):
    alice = paired(text_client, tenant, text_worker, "Alice", "+14155550101", ALICE_TG)
    request = create(text_client, tenant, text_policy(text_client, tenant, [alice["id"]])["id"])
    text_worker.provider.fail_next = [TextProviderError(ErrorKind.PERMANENT, "forbidden")]
    text_worker.run()
    assert prompt_of(session, request["id"]).status == "failed"
    assert events(session, request["id"], "text.prompt_failed")[0]["reason"] == "forbidden"


def test_prompts_lose_their_buttons_once_the_request_is_decided_elsewhere(
    world, text_client, tenant, text_worker, session, session_factory
):
    vote(text_client, tenant, world["request"]["id"], world["alice"]["id"], "approve")
    assert text_service.close_finished_prompts(session_factory, text_worker.provider) == 1
    prompt = prompt_of(session, world["request"]["id"])
    assert (prompt.status, prompt.outcome) == ("closed", "request_closed")
    assert text_worker.provider.message(prompt.provider_message_id)["buttons"] == []


def test_escalation_of_a_text_request_prompts_the_backup_by_text(
    text_client, tenant, text_worker, session, session_factory, mock_provider, text_settings
):
    alice = paired(text_client, tenant, text_worker, "Alice", "+14155550101", ALICE_TG)
    carol = paired(text_client, tenant, text_worker, "Carol", "+14155550103", CAROL_TG)
    policy = text_policy(
        text_client,
        tenant,
        [alice["id"]],
        backup_approver_ids=[carol["id"]],
        decision_deadline_seconds=3600,
        escalation_after_seconds=600,
    )
    request = create(text_client, tenant, policy["id"])
    age_request(session_factory, request["id"], 601)
    run_pending_jobs(session_factory, mock_provider, text_settings, "w1")
    session.expire_all()
    (step,) = session.scalars(select(EscalationStep)).all()
    assert (step.outcome, step.voice_call_id) == ("text_prompt_queued", None)
    assert count(session, VoiceCall) == 0
    text_worker.run()
    carol_prompt = prompt_of(session, request["id"], carol["id"])
    assert (carol_prompt.reason, carol_prompt.status) == ("escalation", "sent")
    # The backup decides the seat by text.
    assert tap(text_worker, carol_prompt, "r", sender=CAROL_TG) == "Rejected: recorded."
    assert get_request(text_client, tenant, request["id"])["status"] == "rejected"


def test_no_path_dials_a_text_request(world, text_settings, session_factory, session):
    with session_factory() as db, transaction(db):
        request = db.get(ApprovalRequest, UUID(world["request"]["id"]), with_for_update=True)
        call, skipped = voice_service.create_call(
            db, text_settings, request, UUID(world["alice"]["id"]), reason="scheduled"
        )
        assert (call, skipped) == (None, "text_channel")
        voice_service.schedule_reapproval(db, text_settings, request, UUID(world["alice"]["id"]))
    (reapproval,) = events(session, world["request"]["id"], "condition.reapproval_requested")
    assert reapproval["call_outcome"] == "text_channel" and reapproval["voice_call_id"] is None
    assert count(session, VoiceCall) == 0


def test_text_then_voice_is_not_available_yet(text_client, tenant):
    alice = create_approver(text_client, tenant, "Alice", phone_number="+14155550101")
    response = post_policy(text_client, tenant, [alice["id"]], approval_channel="text_then_voice")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "approval_channel_unavailable"
    policy = make_policy(text_client, tenant, [alice["id"]])
    assert (policy["approval_channel"], policy["voice_fallback_after_seconds"]) == ("voice", 60)
    patched = text_client.patch(
        f"{API}/policies/{policy['id']}",
        json={"approval_channel": "text_then_voice"},
        headers=tenant.admin,
    )
    assert patched.status_code == 400


def test_set_channel_cli_switches_and_audits(
    text_client, tenant, text_settings, monkeypatch, capsys, session
):
    alice = create_approver(text_client, tenant, "Alice", phone_number="+14155550101")
    policy = make_policy(text_client, tenant, [alice["id"]], auto_dial=True)
    monkeypatch.setattr("app.cli.get_settings", lambda: text_settings)
    assert cli_main(["set-channel", "--policy-id", policy["id"], "--channel", "text"]) == 0
    assert "approval_channel=text" in capsys.readouterr().out
    fetched = text_client.get(f"{API}/policies/{policy['id']}", headers=tenant.admin).json()
    assert (fetched["approval_channel"], fetched["version"]) == ("text", policy["version"] + 1)
    session.expire_all()
    updated = session.scalars(
        select(AuditEvent).where(
            AuditEvent.policy_id == UUID(policy["id"]), AuditEvent.event_type == "policy.updated"
        )
    ).one()
    assert updated.event_data["changed_fields"] == ["approval_channel"]


def test_the_text_worker_refuses_to_start_while_disabled(monkeypatch, test_settings, capsys):
    monkeypatch.setattr(text_worker_module, "get_settings", lambda: test_settings)
    assert text_worker_module.main(["--once"]) == 2
    assert "TEXT_PROVIDER is disabled" in capsys.readouterr().err
