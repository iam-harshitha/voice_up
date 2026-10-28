"""Regressions for the independent Chunk 3 review.

F1: a satisfied condition can only be cleared by its own approver's re-approval, and
    another holder of the same seat can no longer strand the request.
F2: the deadline is judged when the request lock is granted, not when the operation began.
"""

import threading
import time
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select, text

from app.db.base import utcnow
from app.db.models import ApprovalVote
from app.domain.approval_rules import VoteChoice
from app.services import approval_service, voice_webhook_service
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


# --- F1: conditional re-approval across seat holders ---------------------------------------


@pytest.fixture
def seat(voice_client, make_tenant, run_worker, session_factory):
    """Alice and Bob are primaries (unanimous); escalation gives Alice's seat to Backup1
    and Bob's to Backup2. Bob has approved."""
    tenant = make_tenant(
        voice=["voice:calls:create", "voice:calls:read"],
        resolver=["conditions:resolve"],
        auditor=["audit:read"],
    )
    phones = iter(f"+1415555014{i}" for i in range(10))
    people = {
        n: create_approver(voice_client, tenant, n, phone_number=next(phones))["id"]
        for n in ("Alice", "Bob", "Backup1", "Backup2")
    }
    policy = make_policy(
        voice_client,
        tenant,
        [people["Alice"], people["Bob"]],
        backup_approver_ids=[people["Backup1"], people["Backup2"]],
        escalation_after_seconds=AFTER,
    )
    request = create_request(voice_client, tenant, policy["id"])
    age_request(session_factory, request["id"], AFTER + 1)
    run_worker()
    steps = voice_client.get(
        f"{API}/requests/{request['id']}/escalations", headers=tenant.agent
    ).json()
    assert {s["seat_approver_id"]: s["backup_approver_id"] for s in steps} == {
        people["Alice"]: people["Backup1"],
        people["Bob"]: people["Backup2"],
    }
    vote(voice_client, tenant, request["id"], people["Bob"], "approve").raise_for_status()
    return {"tenant": tenant, "id": request["id"], **people}


def add_condition(client, ctx, approver, description="Only after the backup is verified"):
    return client.post(
        f"{API}/requests/{ctx['id']}/conditions",
        json={"approver_id": approver, "description": description},
        headers=ctx["tenant"].voter,
    )


def satisfy(client, ctx, approver):
    condition = add_condition(client, ctx, approver)
    assert condition.status_code == 201, condition.text
    resolved = client.post(
        f"{API}/requests/{ctx['id']}/conditions/{condition.json()['condition']['id']}/resolve",
        json={"outcome": "satisfied", "note": "Backup restore drill passed"},
        headers=ctx["tenant"].headers("resolver"),
    )
    assert resolved.status_code == 200, resolved.text
    return resolved.json()


# (condition stated by, other holder of the same seat)
DIRECTIONS = [("Alice", "Backup1"), ("Backup1", "Alice")]


@pytest.mark.parametrize(("stated_by", "other"), DIRECTIONS)
def test_other_seat_holder_cannot_clear_a_reapproval(voice_client, seat, stated_by, other):
    satisfy(voice_client, seat, seat[stated_by])

    response = vote(voice_client, seat["tenant"], seat["id"], seat[other], "approve")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "reapproval_required"

    # Nothing was written: the seat is still undecided and the request still names the
    # one approver who can move it forward, so the state is never a dead end.
    detail = get_request(voice_client, seat["tenant"], seat["id"])
    assert detail["status"] == "pending"
    assert all(v["seat_approver_id"] != seat["Alice"] for v in detail["votes"])
    evaluation = detail["evaluation"]
    assert evaluation["reason"] == "reapproval_required"
    assert evaluation["next_action"] == "await_decisions"
    assert evaluation["reapproval_approver_ids"] == [seat[stated_by]]
    assert evaluation["reapproval_seat_ids"] == [seat["Alice"]]
    assert evaluation["authorized"] is False


@pytest.mark.parametrize(("stated_by", "other"), DIRECTIONS)
def test_condition_approver_reapproval_completes_the_request(voice_client, seat, stated_by, other):
    satisfy(voice_client, seat, seat[stated_by])
    assert vote(voice_client, seat["tenant"], seat["id"], seat[other], "approve").status_code == 409

    final = vote(voice_client, seat["tenant"], seat["id"], seat[stated_by], "approve")
    assert final.status_code == 201
    body = final.json()
    assert body["request"]["status"] == "approved"
    assert body["vote"]["seat_approver_id"] == seat["Alice"]
    detail = get_request(voice_client, seat["tenant"], seat["id"])
    assert detail["evaluation"]["authorized"] is True
    assert detail["evaluation"]["reapproval_approver_ids"] == []
    types = audit_types(voice_client, seat["tenant"], seat["id"])
    assert types[-1] == "request.approved" and types.count("request.approved") == 1
    # The refused attempt left no trace in the decision record.
    assert types.count("vote.recorded") == 2


@pytest.mark.parametrize(("stated_by", "other"), DIRECTIONS)
def test_other_seat_holder_may_still_reject(voice_client, seat, stated_by, other):
    satisfy(voice_client, seat, seat[stated_by])
    response = vote(voice_client, seat["tenant"], seat["id"], seat[other], "reject")
    assert response.status_code == 201
    assert response.json()["request"]["status"] == "rejected"  # any_rejection veto
    # The satisfied condition is kept as it was, not rewritten.
    conditions = voice_client.get(
        f"{API}/requests/{seat['id']}/conditions", headers=seat["tenant"].agent
    ).json()
    assert [c["status"] for c in conditions] == ["satisfied"]


def test_rejection_of_a_reapproval_seat_does_not_block_a_quorum(
    voice_client, make_tenant, run_worker, session_factory
):
    """Without a veto, a rejected seat is simply decided; its satisfied condition must not
    keep blocking the quorum reached by the other seats."""
    tenant = make_tenant(resolver=["conditions:resolve"])
    people = {n: create_approver(voice_client, tenant, n)["id"] for n in ("A", "B", "C", "Backup")}
    policy = make_policy(
        voice_client,
        tenant,
        [people["A"], people["B"], people["C"]],
        approval_mode="quorum",
        required_approvals=2,
        veto_mode="none",
        backup_approver_ids=[people["Backup"]],
        escalation_after_seconds=AFTER,
    )
    request = create_request(voice_client, tenant, policy["id"])
    age_request(session_factory, request["id"], AFTER + 1)
    run_worker()  # Backup gets seat A (the first outstanding seat)
    ctx = {"tenant": tenant, "id": request["id"]}
    satisfy(voice_client, ctx, people["A"])

    assert vote(voice_client, tenant, request["id"], people["Backup"], "reject").status_code == 201
    vote(voice_client, tenant, request["id"], people["B"], "approve").raise_for_status()
    final = vote(voice_client, tenant, request["id"], people["C"], "approve").json()
    assert final["request"]["status"] == "approved"


def test_only_one_holder_of_a_seat_may_hold_conditions(voice_client, seat):
    first = add_condition(voice_client, seat, seat["Alice"])
    assert first.status_code == 201
    competing = add_condition(voice_client, seat, seat["Backup1"], "Only on a weekday")
    assert competing.status_code == 409
    assert competing.json()["error"]["code"] == "seat_condition_pending"

    # Still refused while Alice's satisfied condition awaits her re-approval.
    voice_client.post(
        f"{API}/requests/{seat['id']}/conditions/{first.json()['condition']['id']}/resolve",
        json={"outcome": "satisfied", "note": "verified"},
        headers=seat["tenant"].headers("resolver"),
    ).raise_for_status()
    competing = add_condition(voice_client, seat, seat["Backup1"], "Only on a weekday")
    assert competing.json()["error"]["code"] == "seat_condition_pending"


def test_not_satisfied_condition_frees_the_seat_for_other_holders(voice_client, seat):
    condition = add_condition(voice_client, seat, seat["Alice"]).json()["condition"]
    voice_client.post(
        f"{API}/requests/{seat['id']}/conditions/{condition['id']}/resolve",
        json={"outcome": "not_satisfied", "note": "drill failed"},
        headers=seat["tenant"].headers("resolver"),
    ).raise_for_status()
    final = vote(voice_client, seat["tenant"], seat["id"], seat["Backup1"], "approve").json()
    assert final["request"]["status"] == "approved"


def test_voice_approval_from_other_holder_is_refused_not_counted(
    voice_client, seat, run_worker, mock_provider, session
):
    satisfy(voice_client, seat, seat["Alice"])
    run_worker()  # dials the escalation and re-approval calls
    backup_call = next(
        c
        for c in list_calls(voice_client, seat["tenant"], seat["id"])
        if c["approver_id"] == seat["Backup1"]
    )
    deliver_webhook(
        voice_client, mock_provider.complete(backup_call["provider_call_id"]), WEBHOOK_TOKEN
    )
    assert ("process_event", "reapproval_required") in run_worker()
    backup_call = next(
        c
        for c in list_calls(voice_client, seat["tenant"], seat["id"])
        if c["approver_id"] == seat["Backup1"]
    )
    assert backup_call["decision_status"] == "refused"
    detail = get_request(voice_client, seat["tenant"], seat["id"])
    assert detail["status"] == "pending"
    assert detail["evaluation"]["reapproval_approver_ids"] == [seat["Alice"]]


# --- F2: the deadline is judged when the request lock is granted ---------------------------

DEADLINE = 3600


class Clock:
    """Real time plus an offset the test moves forward at a chosen moment."""

    def __init__(self):
        self.offset = timedelta(0)

    def __call__(self):
        return utcnow() + self.offset


def hold_request_lock(session_factory, request_id):
    db = session_factory()
    db.begin()
    db.execute(text("SELECT id FROM approval_requests WHERE id = :r FOR UPDATE"), {"r": request_id})
    return db


def wait_until_blocked_on_lock(session, timeout=20.0):
    """Deterministic synchronisation: poll until another backend waits on a row lock."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waiting = session.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        )
        if waiting:
            return
        time.sleep(0.02)
    raise AssertionError("the operation never blocked on the request lock")


def test_vote_that_gets_the_lock_after_the_deadline_is_refused(
    client, tenant, session_factory, session, monkeypatch
):
    alice = create_approver(client, tenant, "Alice")["id"]
    policy = make_policy(client, tenant, [alice], decision_deadline_seconds=DEADLINE)
    request = create_request(client, tenant, policy["id"])
    clock = Clock()
    monkeypatch.setattr(approval_service, "utcnow", clock)

    holder = hold_request_lock(session_factory, request["id"])
    result: dict[str, object] = {}

    def cast():  # begins well before the deadline
        with session_factory() as db:
            try:
                approval_service.cast_vote(
                    db, tenant.organization_id, UUID(request["id"]), UUID(alice), VoteChoice.APPROVE
                )
                result["vote"] = "ok"
            except Exception as exc:  # asserted below
                result["vote"] = getattr(exc, "code", type(exc).__name__)

    worker = threading.Thread(target=cast)
    worker.start()
    try:
        wait_until_blocked_on_lock(session)
        clock.offset = timedelta(seconds=DEADLINE + 5)  # the deadline passes while it waits
    finally:
        holder.commit()
        holder.close()
    worker.join(30)

    assert result["vote"] == "request_expired"
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(ApprovalVote)) == 0


def test_condition_that_gets_the_lock_after_the_deadline_is_refused(
    client, tenant, session_factory, session, monkeypatch
):
    alice = create_approver(client, tenant, "Alice")["id"]
    policy = make_policy(client, tenant, [alice], decision_deadline_seconds=DEADLINE)
    request = create_request(client, tenant, policy["id"])
    clock = Clock()
    monkeypatch.setattr(approval_service, "utcnow", clock)

    holder = hold_request_lock(session_factory, request["id"])
    result: dict[str, object] = {}

    def record():
        with session_factory() as db:
            try:
                approval_service.create_condition(
                    db, tenant.organization_id, UUID(request["id"]), UUID(alice), "After review"
                )
                result["condition"] = "ok"
            except Exception as exc:  # asserted below
                result["condition"] = getattr(exc, "code", type(exc).__name__)

    worker = threading.Thread(target=record)
    worker.start()
    try:
        wait_until_blocked_on_lock(session)
        clock.offset = timedelta(seconds=DEADLINE + 5)
    finally:
        holder.commit()
        holder.close()
    worker.join(30)

    assert result["condition"] == "request_expired"


def test_voice_decision_that_gets_the_lock_after_the_deadline_is_refused(
    voice_client, make_tenant, mock_provider, run_worker, session_factory, session, monkeypatch
):
    tenant = make_tenant(voice=["voice:calls:create", "voice:calls:read"])
    alice = create_approver(voice_client, tenant, "Alice", phone_number="+14155550151")["id"]
    policy = make_policy(voice_client, tenant, [alice], decision_deadline_seconds=DEADLINE)
    request = create_request(voice_client, tenant, policy["id"])
    schedule_calls(voice_client, tenant, request["id"])
    run_worker()
    call = list_calls(voice_client, tenant, request["id"])[0]
    deliver_webhook(voice_client, mock_provider.complete(call["provider_call_id"]), WEBHOOK_TOKEN)

    clock = Clock()
    monkeypatch.setattr(approval_service, "utcnow", clock)
    monkeypatch.setattr(voice_webhook_service, "utcnow", clock)
    holder = hold_request_lock(session_factory, request["id"])
    results: list = []
    worker = threading.Thread(target=lambda: results.extend(run_worker()))
    worker.start()
    try:
        wait_until_blocked_on_lock(session)
        clock.offset = timedelta(seconds=DEADLINE + 5)
    finally:
        holder.commit()
        holder.close()
    worker.join(30)

    assert ("process_event", "request_expired") in results
    call = list_calls(voice_client, tenant, request["id"])[0]
    assert call["decision_status"] == "refused"
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(ApprovalVote)) == 0
