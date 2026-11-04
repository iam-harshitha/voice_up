"""text_then_voice: a Telegram prompt first, the call only if the seat is still undecided
after voice_fallback_after_seconds. Mock mode only (no network).

The voice worker runs with a simulated clock (``later=``) so the delayed call becomes due
without waiting; taps go through the real text worker loop.
"""

from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, update

from app.cli import main as cli_main
from app.db.base import utcnow
from app.db.models import ApprovalRequest, ApprovalVote, TextPrompt, VoiceCall, VoiceJob
from app.db.models.voice import JobType
from app.text.base import ErrorKind, TextProviderError
from tests.conftest import WEBHOOK_TOKEN
from tests.helpers import (
    API,
    create_approver,
    deliver_webhook,
    get_request,
    make_policy,
    post_policy,
)
from tests.integration.test_text_approvals import (
    ALICE_TG,
    NOT_ACCEPTED,
    count,
    create,
    events,
    paired,
    prompt_of,
    rejected_reasons,
    tap,
)

pytestmark = pytest.mark.integration

FALLBACK = 10


@pytest.fixture
def tenant(make_tenant):
    return make_tenant(voice=["voice:calls:create", "voice:calls:read"])


def ttv_policy(client, tenant, approver_ids, **fields):
    return make_policy(
        client,
        tenant,
        approver_ids,
        auto_dial=True,
        approval_channel="text_then_voice",
        voice_fallback_after_seconds=FALLBACK,
        **fields,
    )


def calls(session, request_id: str, approver_id: str | None = None) -> list[VoiceCall]:
    session.expire_all()
    query = select(VoiceCall).where(VoiceCall.request_id == UUID(request_id))
    if approver_id:
        query = query.where(VoiceCall.approver_id == UUID(approver_id))
    return list(session.scalars(query))


def initiate_job(session, call: VoiceCall) -> VoiceJob:
    session.expire_all()
    return session.scalars(
        select(VoiceJob).where(
            VoiceJob.voice_call_id == call.id, VoiceJob.job_type == JobType.INITIATE_CALL
        )
    ).one()


def votes(session, request_id: str) -> int:
    return count(session, ApprovalVote, ApprovalVote.request_id == UUID(request_id))


def answer_call(client, mock_provider, run_worker, call: VoiceCall, decision: str) -> None:
    payload = mock_provider.complete(call.provider_call_id, decision=decision)
    assert deliver_webhook(client, payload, WEBHOOK_TOKEN).status_code == 200
    run_worker(later=FALLBACK + 5)


@pytest.fixture
def world(text_client, tenant, text_worker, session):
    """Alice (paired) on a text_then_voice policy; the prompt is delivered."""
    alice = paired(text_client, tenant, text_worker, "Alice", "+14155550101", ALICE_TG)
    policy = ttv_policy(text_client, tenant, [alice["id"]])
    request = create(text_client, tenant, policy["id"])
    text_worker.run()
    return {
        "alice": alice,
        "policy": policy,
        "request": request,
        "prompt": prompt_of(session, request["id"]),
    }


# --- Creation ------------------------------------------------------------------------------


def test_creation_sends_the_text_now_and_delays_the_call(world, session, mock_provider, run_worker):
    request_id = world["request"]["id"]
    assert world["prompt"].status == "sent"
    (call,) = calls(session, request_id)
    assert call.status == "queued"
    job = initiate_job(session, call)
    delay = (job.run_after - utcnow()).total_seconds()
    assert FALLBACK - 3 < delay <= FALLBACK
    (scheduled,) = events(session, request_id, "voice.call_scheduled")
    assert "start_after" in scheduled
    (auto,) = events(session, request_id, "voice.auto_dial_scheduled")
    assert auto["delayed_approver_ids"] == [world["alice"]["id"]]
    session.expire_all()
    assert session.get(ApprovalRequest, UUID(request_id)).policy_snapshot["approval_channel"] == (
        "text_then_voice"
    )
    run_worker()  # before the delay: nothing is dialled
    assert mock_provider.initiated == []


def test_an_approver_without_a_link_is_called_at_once(
    text_client, tenant, session, mock_provider, run_worker
):
    bob = create_approver(text_client, tenant, "Bob", phone_number="+14155550102")
    request = create(text_client, tenant, ttv_policy(text_client, tenant, [bob["id"]])["id"])
    assert len(calls(session, request["id"])) == 1
    assert "start_after" not in events(session, request["id"], "voice.call_scheduled")[0]
    assert events(session, request["id"], "text.prompt_failed")[0]["reason"] == "no_verified_link"
    assert (
        "delayed_approver_ids" not in events(session, request["id"], "voice.auto_dial_scheduled")[0]
    )
    run_worker()
    assert [c.recipient_phone_number for c in mock_provider.initiated] == ["+14155550102"]


def test_text_disabled_means_an_immediate_call(
    voice_client, tenant, session, mock_provider, run_worker
):
    alice = create_approver(voice_client, tenant, "Alice", phone_number="+14155550101")
    request = create(voice_client, tenant, ttv_policy(voice_client, tenant, [alice["id"]])["id"])
    assert count(session, TextPrompt) == 0
    assert events(session, request["id"], "text.prompt_failed")[0]["reason"] == "text_unavailable"
    run_worker()
    assert len(mock_provider.initiated) == 1


# --- The fallback ------------------------------------------------------------------------------


def test_a_text_decision_before_the_fallback_cancels_the_call(
    world, text_client, tenant, text_worker, session, mock_provider, run_worker
):
    tap(text_worker, world["prompt"], "a")
    assert (
        tap(text_worker, prompt_of(session, world["request"]["id"]), "c") == "Approved: recorded."
    )
    assert get_request(text_client, tenant, world["request"]["id"])["status"] == "approved"
    run_worker(later=FALLBACK + 1)
    (call,) = calls(session, world["request"]["id"])
    assert (call.status, call.outcome) == ("canceled", "request_approved")
    assert mock_provider.initiated == []


def test_the_call_is_cancelled_when_only_the_approvers_seat_has_voted(
    text_client, tenant, text_worker, session, mock_provider, run_worker
):
    """The weak point: the request is still pending (Bob has not decided), but Alice already
    answered by text, so her delayed call must not ring."""
    alice = paired(text_client, tenant, text_worker, "Alice", "+14155550101", ALICE_TG)
    bob = create_approver(text_client, tenant, "Bob", phone_number="+14155550102")
    request = create(
        text_client, tenant, ttv_policy(text_client, tenant, [alice["id"], bob["id"]])["id"]
    )
    text_worker.run()
    prompt = prompt_of(session, request["id"], alice["id"])
    tap(text_worker, prompt, "a")
    tap(text_worker, prompt_of(session, request["id"], alice["id"]), "c")
    assert get_request(text_client, tenant, request["id"])["status"] == "pending"
    run_worker(later=FALLBACK + 1)
    (alice_call,) = calls(session, request["id"], alice["id"])
    assert (alice_call.status, alice_call.outcome) == ("canceled", "seat_decided")
    # Bob had no Telegram link, so he was called at once; Alice never.
    assert [c.recipient_phone_number for c in mock_provider.initiated] == ["+14155550102"]


def test_no_text_decision_places_the_call_after_the_delay(
    world, session, mock_provider, run_worker
):
    run_worker(later=FALLBACK - 5)
    assert mock_provider.initiated == []
    run_worker(later=FALLBACK + 1)
    assert [c.recipient_phone_number for c in mock_provider.initiated] == ["+14155550101"]
    (call,) = calls(session, world["request"]["id"])
    assert call.status == "initiated"


def test_a_failed_text_send_starts_the_call_at_once(
    text_client, tenant, text_worker, session, mock_provider, run_worker
):
    alice = paired(text_client, tenant, text_worker, "Alice", "+14155550101", ALICE_TG)
    request = create(text_client, tenant, ttv_policy(text_client, tenant, [alice["id"]])["id"])
    text_worker.provider.fail_next = [TextProviderError(ErrorKind.PERMANENT, "forbidden")]
    text_worker.run()
    (expedited,) = events(session, request["id"], "voice.fallback_expedited")
    assert expedited["reason"] == "forbidden"
    run_worker()  # now, not after the fallback delay
    assert len(mock_provider.initiated) == 1


def test_a_retryable_text_failure_also_starts_the_call_at_once(
    text_client, tenant, text_worker, session, mock_provider, run_worker
):
    alice = paired(text_client, tenant, text_worker, "Alice", "+14155550101", ALICE_TG)
    request = create(text_client, tenant, ttv_policy(text_client, tenant, [alice["id"]])["id"])
    text_worker.provider.fail_next = [TextProviderError(ErrorKind.RETRYABLE, "timeout")]
    text_worker.run()
    assert prompt_of(session, request["id"]).status == "queued"  # the text is retried too
    run_worker()
    assert len(mock_provider.initiated) == 1


# --- Both channels answer: the first valid vote wins --------------------------------------------


def started_call(world, session, run_worker) -> VoiceCall:
    run_worker(later=FALLBACK + 1)
    (call,) = calls(session, world["request"]["id"])
    assert call.status == "initiated"
    return call


def test_a_text_reject_after_the_call_started_wins_and_the_call_answer_is_refused(
    world, text_client, tenant, text_worker, session, mock_provider, run_worker
):
    call = started_call(world, session, run_worker)
    assert tap(text_worker, world["prompt"], "r") == "Rejected: recorded."
    answer_call(text_client, mock_provider, run_worker, call, "approve")
    session.expire_all()
    call = session.get(VoiceCall, call.id)
    assert (call.decision_status, call.outcome) == ("refused", "vote_conflict")
    assert votes(session, world["request"]["id"]) == 1
    assert get_request(text_client, tenant, world["request"]["id"])["status"] == "rejected"
    assert events(session, world["request"]["id"], "voice.decision_rejected")[0]["reason"] == (
        "vote_conflict"
    )


def test_the_same_decision_by_text_then_by_phone_is_recorded_once(
    world, text_client, tenant, text_worker, session, mock_provider, run_worker
):
    call = started_call(world, session, run_worker)
    tap(text_worker, world["prompt"], "a")
    assert (
        tap(text_worker, prompt_of(session, world["request"]["id"]), "c") == "Approved: recorded."
    )
    answer_call(text_client, mock_provider, run_worker, call, "approve")
    session.expire_all()
    assert session.get(VoiceCall, call.id).outcome == "vote_already_recorded"
    assert votes(session, world["request"]["id"]) == 1
    status = get_request(text_client, tenant, world["request"]["id"])
    assert status["status"] == "approved" and status["evaluation"]["authorized"] is True


def test_a_phone_answer_then_a_text_tap_keeps_the_phone_decision(
    world, text_client, tenant, text_worker, session, mock_provider, run_worker
):
    call = started_call(world, session, run_worker)
    answer_call(text_client, mock_provider, run_worker, call, "reject")
    assert get_request(text_client, tenant, world["request"]["id"])["status"] == "rejected"
    tap(text_worker, world["prompt"], "a")  # the Approve button is still on screen
    assert text_worker.provider.answers[-1][1] == NOT_ACCEPTED
    assert rejected_reasons(session, world["request"]["id"]) == ["request_closed"]
    assert votes(session, world["request"]["id"]) == 1


def test_a_phone_answer_then_a_conflicting_tap_on_a_pending_request(
    text_client, tenant, text_worker, session, mock_provider, run_worker
):
    alice = paired(text_client, tenant, text_worker, "Alice", "+14155550101", ALICE_TG)
    bob = create_approver(text_client, tenant, "Bob", phone_number="+14155550102")
    request = create(
        text_client, tenant, ttv_policy(text_client, tenant, [alice["id"], bob["id"]])["id"]
    )
    text_worker.run()
    run_worker(later=FALLBACK + 1)
    (alice_call,) = calls(session, request["id"], alice["id"])
    answer_call(text_client, mock_provider, run_worker, alice_call, "approve")
    assert get_request(text_client, tenant, request["id"])["status"] == "pending"  # Bob next
    assert tap(text_worker, prompt_of(session, request["id"], alice["id"]), "r") == NOT_ACCEPTED
    assert rejected_reasons(session, request["id"]) == ["vote_conflict"]
    assert votes(session, request["id"]) == 1


# --- Configuration ---------------------------------------------------------------------


@pytest.mark.parametrize(("seconds", "ok"), [(4, False), (5, True), (600, True), (601, False)])
def test_fallback_bounds(text_client, tenant, seconds, ok):
    alice = create_approver(text_client, tenant, "Alice", phone_number="+14155550101")
    response = post_policy(
        text_client,
        tenant,
        [alice["id"]],
        approval_channel="text_then_voice",
        voice_fallback_after_seconds=seconds,
    )
    assert (response.status_code == 201) is ok, response.text


def test_set_channel_cli_accepts_text_then_voice_with_a_fallback(
    text_client, tenant, text_settings, monkeypatch, capsys
):
    alice = create_approver(text_client, tenant, "Alice", phone_number="+14155550101")
    policy = make_policy(text_client, tenant, [alice["id"]], auto_dial=True)
    monkeypatch.setattr("app.cli.get_settings", lambda: text_settings)
    argv = ["set-channel", "--policy-id", policy["id"], "--channel", "text_then_voice"]
    assert cli_main([*argv, "--fallback-seconds", "10"]) == 0
    assert "called" in capsys.readouterr().out
    fetched = text_client.get(f"{API}/policies/{policy['id']}", headers=tenant.admin).json()
    assert (fetched["approval_channel"], fetched["voice_fallback_after_seconds"]) == (
        "text_then_voice",
        10,
    )
    with pytest.raises(SystemExit):
        cli_main([*argv, "--fallback-seconds", "4"])


def test_escalation_of_a_text_then_voice_request_calls_the_backup(
    text_client, tenant, text_worker, session, session_factory, mock_provider, text_settings
):
    from app.services.voice_job_service import run_pending_jobs
    from tests.helpers import age_request

    alice = paired(text_client, tenant, text_worker, "Alice", "+14155550101", ALICE_TG)
    carol = create_approver(text_client, tenant, "Carol", phone_number="+14155550103")
    policy = ttv_policy(
        text_client,
        tenant,
        [alice["id"]],
        backup_approver_ids=[carol["id"]],
        decision_deadline_seconds=3600,
        escalation_after_seconds=600,
    )
    request = create(text_client, tenant, policy["id"])
    age_request(session_factory, request["id"], 601)
    with session_factory() as db:  # keep Alice's delayed call out of this check
        db.execute(
            update(VoiceJob)
            .where(VoiceJob.request_id.is_(None))
            .values(run_after=utcnow() + timedelta(hours=1))
        )
        db.commit()
    run_pending_jobs(session_factory, mock_provider, text_settings, "w1")
    assert [c.recipient_phone_number for c in mock_provider.initiated] == ["+14155550103"]
