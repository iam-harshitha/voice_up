"""Durable, bounded escalation to backup approvers."""

import threading
from uuid import UUID

import pytest
from sqlalchemy import func, select, text

from app.db.models import EscalationStep, VoiceCall, VoiceJob
from app.services.voice_job_service import claim_next, run_pending_jobs
from tests.conftest import WEBHOOK_TOKEN
from tests.helpers import (
    API,
    age_request,
    audit_types,
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

AFTER = 600


@pytest.fixture
def esc(voice_client, make_tenant):
    tenant = make_tenant(voice=["voice:calls:create", "voice:calls:read"], auditor=["audit:read"])
    phones = iter(f"+1415555012{i}" for i in range(10))
    names = ("Alice", "Bob", "Backup1", "Backup2", "Backup3")
    people = {
        n: create_approver(voice_client, tenant, n, phone_number=next(phones))["id"] for n in names
    }
    return {"tenant": tenant, **people}


def escalating_request(
    client, esc, *, deadline=None, backups=("Backup1", "Backup2"), max_escalations=None, **fields
):
    body = {
        "backup_approver_ids": [esc[b] for b in backups],
        "escalation_after_seconds": AFTER,
        **fields,
    }
    if deadline:
        body["decision_deadline_seconds"] = deadline
    if max_escalations is not None:
        body["max_escalations"] = max_escalations
    policy = make_policy(client, esc["tenant"], [esc["Alice"], esc["Bob"]], **body)
    return create_request(client, esc["tenant"], policy["id"])


def steps(client, esc, request_id):
    response = client.get(f"{API}/requests/{request_id}/escalations", headers=esc["tenant"].agent)
    assert response.status_code == 200
    return response.json()


def test_no_escalation_before_the_trigger(voice_client, esc, run_worker):
    request = escalating_request(voice_client, esc)
    assert run_worker() == []  # escalate job is scheduled for created_at + AFTER
    assert steps(voice_client, esc, request["id"]) == []


def test_undecided_seats_get_backups_in_order(
    voice_client, esc, run_worker, session_factory, mock_provider
):
    request = escalating_request(voice_client, esc)
    vote(voice_client, esc["tenant"], request["id"], esc["Alice"], "approve")
    age_request(session_factory, request["id"], AFTER + 1)
    results = run_worker()
    assert ("escalate_request", "escalated_1") in results
    # The backup's call job was created during that pass; the next poll dials it.
    run_worker()
    [step] = steps(voice_client, esc, request["id"])
    # Only Bob's seat was undecided; it goes to the first backup.
    assert (step["seat_approver_id"], step["backup_approver_id"]) == (esc["Bob"], esc["Backup1"])
    assert step["outcome"] == "call_scheduled" and step["voice_call_id"]
    assert [c.recipient_phone_number for c in mock_provider.initiated][-1:] == [
        voice_client.get(f"{API}/approvers/{esc['Backup1']}", headers=esc["tenant"].admin).json()[
            "phone_number"
        ]
    ]


def test_backup_replaces_the_seat_and_quorum_is_unchanged(
    voice_client, esc, run_worker, session_factory
):
    request = escalating_request(voice_client, esc)
    age_request(session_factory, request["id"], AFTER + 1)
    run_worker()
    assigned = {
        s["seat_approver_id"]: s["backup_approver_id"]
        for s in steps(voice_client, esc, request["id"])
    }
    assert assigned == {esc["Alice"]: esc["Backup1"], esc["Bob"]: esc["Backup2"]}
    # One backup approving is still only one of two required approvals.
    first = vote(voice_client, esc["tenant"], request["id"], esc["Backup1"], "approve").json()
    assert first["request"]["status"] == "pending"
    # The primary of an already-decided seat cannot decide it again.
    late = vote(voice_client, esc["tenant"], request["id"], esc["Alice"], "reject")
    assert late.status_code == 409 and late.json()["error"]["code"] == "seat_already_decided"
    # The other seat decided by its primary (still allowed) completes the quorum.
    final = vote(voice_client, esc["tenant"], request["id"], esc["Bob"], "approve").json()
    assert final["request"]["status"] == "approved"
    # Bob's backup can no longer decide his seat.
    assert (
        vote(voice_client, esc["tenant"], request["id"], esc["Backup2"], "reject").status_code
        == 409
    )


def test_backup_rejection_on_a_seat_vetoes(voice_client, esc, run_worker, session_factory):
    request = escalating_request(voice_client, esc)
    age_request(session_factory, request["id"], AFTER + 1)
    run_worker()
    result = vote(voice_client, esc["tenant"], request["id"], esc["Backup2"], "reject").json()
    assert result["request"]["status"] == "rejected"


def test_unassigned_backup_cannot_vote(voice_client, esc):
    request = escalating_request(voice_client, esc)
    response = vote(voice_client, esc["tenant"], request["id"], esc["Backup1"], "approve")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "approver_not_authorized"


def test_max_escalations_and_backups_bound_the_chain(
    voice_client, esc, run_worker, session_factory, session
):
    request = escalating_request(
        voice_client, esc, backups=("Backup1", "Backup2", "Backup3"), max_escalations=2
    )
    age_request(session_factory, request["id"], 3 * AFTER + 1)
    run_worker()
    run_worker(later=10 * AFTER)
    assert len(steps(voice_client, esc, request["id"])) == 2
    session.expire_all()
    job = session.scalars(
        select(VoiceJob).where(
            VoiceJob.request_id == UUID(request["id"]), VoiceJob.job_type == "escalate_request"
        )
    ).one()
    assert job.status == "succeeded"
    assert "escalation.exhausted" in audit_types(voice_client, esc["tenant"], request["id"])


def test_rounds_continue_until_backups_are_used(voice_client, esc, run_worker, session_factory):
    request = escalating_request(voice_client, esc, backups=("Backup1", "Backup2", "Backup3"))
    vote(voice_client, esc["tenant"], request["id"], esc["Alice"], "approve")
    age_request(session_factory, request["id"], AFTER + 1)
    run_worker()  # round 1: Bob's seat -> Backup1
    run_worker(later=AFTER)  # round 2: Bob's seat still undecided -> Backup2
    chain = steps(voice_client, esc, request["id"])
    assert [(s["round"], s["seat_approver_id"], s["backup_approver_id"]) for s in chain] == [
        (1, esc["Bob"], esc["Backup1"]),
        (2, esc["Bob"], esc["Backup2"]),
    ]
    # Either assigned backup may decide the seat; the first valid vote wins.
    vote(voice_client, esc["tenant"], request["id"], esc["Backup2"], "approve")
    assert get_request(voice_client, esc["tenant"], request["id"])["status"] == "approved"


def test_round_is_idempotent(voice_client, esc, run_worker, session_factory, session):
    request = escalating_request(voice_client, esc)
    age_request(session_factory, request["id"], AFTER + 1)
    run_worker()
    with session_factory() as db:  # a stale re-run of the same round
        db.execute(
            text(
                "UPDATE voice_jobs SET status='pending', run_after=now() "
                "WHERE request_id=:r AND job_type='escalate_request'"
            ),
            {"r": request["id"]},
        )
        db.commit()
    results = [r for t, r in run_worker() if t == "escalate_request"]
    assert results in (["round_already_done"], [])
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(EscalationStep)) == 2


def test_deadline_takes_precedence(voice_client, esc, run_worker, session_factory):
    request = escalating_request(voice_client, esc, deadline=AFTER + 300)
    age_request(session_factory, request["id"], AFTER + 301)  # past both
    results = dict(run_worker())
    assert results["expire_request"] == "now_expired"
    assert results["escalate_request"] in ("deadline_passed", "request_expired")
    assert steps(voice_client, esc, request["id"]) == []


def test_terminal_request_never_escalates(voice_client, esc, run_worker, session_factory):
    request = escalating_request(voice_client, esc)
    vote(voice_client, esc["tenant"], request["id"], esc["Alice"], "reject")
    age_request(session_factory, request["id"], AFTER + 1)
    assert dict(run_worker())["escalate_request"] == "request_rejected"
    assert steps(voice_client, esc, request["id"]) == []


def test_seat_with_open_condition_is_not_escalated(voice_client, esc, run_worker, session_factory):
    request = escalating_request(voice_client, esc)
    voice_client.post(
        f"{API}/requests/{request['id']}/conditions",
        json={"approver_id": esc["Alice"], "description": "after QA"},
        headers=esc["tenant"].voter,
    ).raise_for_status()
    age_request(session_factory, request["id"], AFTER + 1)
    run_worker()
    assert [s["seat_approver_id"] for s in steps(voice_client, esc, request["id"])] == [esc["Bob"]]


def test_escalation_without_voice_still_authorizes_the_backup(
    client, make_tenant, session_factory, test_settings
):
    tenant = make_tenant()
    a, b = (create_approver(client, tenant, n)["id"] for n in ("A", "B"))
    policy = make_policy(
        client, tenant, [a], backup_approver_ids=[b], escalation_after_seconds=AFTER
    )
    request = create_request(client, tenant, policy["id"])
    age_request(session_factory, request["id"], AFTER + 1)
    assert run_pending_jobs(session_factory, None, test_settings, "w") == [
        ("escalate_request", "escalated_1_exhausted")
    ]
    [step] = client.get(f"{API}/requests/{request['id']}/escalations", headers=tenant.agent).json()
    assert step["outcome"] == "voice_unavailable" and step["voice_call_id"] is None
    assert (
        vote(client, tenant, request["id"], b, "approve").json()["request"]["status"] == "approved"
    )


def test_worker_crash_after_claim_recovers_the_round(
    voice_client, esc, session_factory, voice_settings, mock_provider, run_worker
):
    request = escalating_request(voice_client, esc)
    age_request(session_factory, request["id"], AFTER + 1)
    with session_factory() as db:
        claimed = claim_next(db, voice_settings, "dead-worker")
    assert claimed is not None and claimed.job_type == "escalate_request"
    assert steps(voice_client, esc, request["id"]) == []
    run_worker(later=voice_settings.voice_job_lease_seconds + 1)
    assert len(steps(voice_client, esc, request["id"])) == 2


def test_concurrent_workers_create_one_round(
    voice_client, esc, session_factory, voice_settings, mock_provider, session
):
    request = escalating_request(voice_client, esc)
    age_request(session_factory, request["id"], AFTER + 1)
    barrier = threading.Barrier(4)

    def work(n):
        barrier.wait()
        run_pending_jobs(session_factory, mock_provider, voice_settings, f"w{n}")

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(EscalationStep)) == 2
    backups = {UUID(esc["Backup1"]), UUID(esc["Backup2"])}
    calls = session.scalars(select(VoiceCall).where(VoiceCall.approver_id.in_(backups))).all()
    assert len(calls) == 2 and len(mock_provider.initiated) == 2


def test_voice_decision_from_escalated_backup_counts_for_the_seat(
    voice_client, esc, run_worker, session_factory, mock_provider
):
    request = escalating_request(voice_client, esc)
    schedule_calls(voice_client, esc["tenant"], request["id"])
    run_worker()
    vote(voice_client, esc["tenant"], request["id"], esc["Alice"], "approve")
    age_request(session_factory, request["id"], AFTER + 1)
    run_worker()  # escalation round (creates the backup's call job)
    run_worker()  # next poll: initiates the backup call
    backup_call = next(
        c
        for c in list_calls(voice_client, esc["tenant"], request["id"])
        if c["approver_id"] == esc["Backup1"]
    )
    deliver_webhook(
        voice_client, mock_provider.complete(backup_call["provider_call_id"]), WEBHOOK_TOKEN
    )
    run_worker()
    detail = get_request(voice_client, esc["tenant"], request["id"])
    assert detail["status"] == "approved"
    backup_vote = next(v for v in detail["votes"] if v["approver_id"] == esc["Backup1"])
    assert backup_vote["seat_approver_id"] == esc["Bob"]


def test_escalation_steps_are_immutable(voice_client, esc, run_worker, session_factory, session):
    request = escalating_request(voice_client, esc)
    age_request(session_factory, request["id"], AFTER + 1)
    run_worker()
    with pytest.raises(Exception, match="immutable"):
        session.execute(text("UPDATE escalation_steps SET outcome = 'x'"))
    session.rollback()
