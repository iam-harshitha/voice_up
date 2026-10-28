"""Conditional approvals: never a vote, block approval, resolved only by authorized actors."""

from uuid import UUID

import pytest
from sqlalchemy import func, select, text

from app.db.models import ApprovalVote, VoiceCall
from app.services import approval_service
from tests.conftest import WEBHOOK_TOKEN
from tests.helpers import (
    API,
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


@pytest.fixture
def ctx(client, make_tenant):
    tenant = make_tenant(resolver=["conditions:resolve"], auditor=["audit:read"])
    alice = create_approver(client, tenant, "Alice")["id"]
    bob = create_approver(client, tenant, "Bob")["id"]
    policy = make_policy(client, tenant, [alice, bob])
    request = create_request(client, tenant, policy["id"])
    return {"tenant": tenant, "alice": alice, "bob": bob, "id": request["id"]}


def add_condition(client, ctx, approver, description="Only after the backup is verified"):
    return client.post(
        f"{API}/requests/{ctx['id']}/conditions",
        json={"approver_id": approver, "description": description},
        headers=ctx["tenant"].voter,
    )


def resolve(client, ctx, condition_id, outcome="satisfied", role="resolver", **extra):
    return client.post(
        f"{API}/requests/{ctx['id']}/conditions/{condition_id}/resolve",
        json={"outcome": outcome, "note": "Backup restore drill passed", **extra},
        headers=ctx["tenant"].headers(role),
    )


def votes(session):
    session.expire_all()
    return session.scalar(select(func.count()).select_from(ApprovalVote))


def test_condition_is_not_a_vote_and_blocks_approval(client, ctx, session):
    response = add_condition(client, ctx, ctx["alice"])
    assert response.status_code == 201
    body = response.json()
    assert body["condition"]["status"] == "open" and body["created"] is True
    assert body["request"]["status"] == "pending"
    evaluation = body["request"]["evaluation"]
    assert (evaluation["state"], evaluation["next_action"], evaluation["authorized"]) == (
        "waiting_for_condition",
        "resolve_condition",
        False,
    )
    assert votes(session) == 0
    # Bob approves: quorum would need Alice anyway, and her seat is blocked.
    vote(client, ctx["tenant"], ctx["id"], ctx["bob"], "approve")
    assert get_request(client, ctx["tenant"], ctx["id"])["status"] == "pending"


def test_condition_blocks_approval_even_when_quorum_is_met(client, make_tenant):
    tenant = make_tenant()
    ids = [create_approver(client, tenant, n)["id"] for n in ("A", "B", "C")]
    policy = make_policy(client, tenant, ids, approval_mode="quorum", required_approvals=2)
    request = create_request(client, tenant, policy["id"])
    ctx = {"tenant": tenant, "id": request["id"]}
    add_condition(client, ctx, ids[2])
    vote(client, tenant, request["id"], ids[0], "approve")
    result = vote(client, tenant, request["id"], ids[1], "approve").json()
    assert result["request"]["status"] == "pending"
    evaluation = get_request(client, tenant, request["id"])["evaluation"]
    assert evaluation["approvals"] == 2 and evaluation["reason"] == "condition_open"


def test_approving_while_condition_is_open_is_refused_but_rejecting_is_not(client, ctx, session):
    add_condition(client, ctx, ctx["alice"])
    refused = vote(client, ctx["tenant"], ctx["id"], ctx["alice"], "approve")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "condition_open"
    assert votes(session) == 0
    rejected = vote(client, ctx["tenant"], ctx["id"], ctx["alice"], "reject").json()
    assert rejected["request"]["status"] == "rejected"
    conditions = client.get(
        f"{API}/requests/{ctx['id']}/conditions", headers=ctx["tenant"].agent
    ).json()
    assert conditions[0]["status"] == "superseded"


def test_satisfied_condition_requires_reapproval(client, ctx, session):
    condition = add_condition(client, ctx, ctx["alice"]).json()["condition"]
    vote(client, ctx["tenant"], ctx["id"], ctx["bob"], "approve")
    resolved = resolve(client, ctx, condition["id"], evidence_reference="CHG-1042").json()
    assert resolved["condition"]["status"] == "satisfied"
    assert resolved["condition"]["resolved_by_api_key_id"] == str(ctx["tenant"].key_ids["resolver"])
    # The resolution itself does not approve anything.
    assert resolved["request"]["status"] == "pending"
    evaluation = resolved["request"]["evaluation"]
    assert (evaluation["reason"], evaluation["reapproval_approver_ids"]) == (
        "reapproval_required",
        [ctx["alice"]],
    )
    assert evaluation["next_action"] == "await_decisions"
    # Alice's fresh unconditional approval is what completes the decision.
    final = vote(client, ctx["tenant"], ctx["id"], ctx["alice"], "approve").json()
    assert final["request"]["status"] == "approved"
    timeline = audit_types(client, ctx["tenant"], ctx["id"])
    assert timeline.index("condition.created") < timeline.index("condition.resolved")
    assert timeline.index("condition.resolved") < timeline.index("condition.reapproval_requested")
    assert timeline[-1] == "request.approved"


def test_not_satisfied_returns_the_seat_to_undecided(client, ctx):
    condition = add_condition(client, ctx, ctx["alice"]).json()["condition"]
    resolved = resolve(client, ctx, condition["id"], outcome="not_satisfied").json()
    evaluation = resolved["request"]["evaluation"]
    assert evaluation["state"] == "awaiting_approvals"
    assert ctx["alice"] in evaluation["outstanding_approver_ids"]


def test_resolution_is_idempotent_and_conflicts_are_refused(client, ctx):
    condition = add_condition(client, ctx, ctx["alice"]).json()["condition"]
    assert resolve(client, ctx, condition["id"]).status_code == 200
    again = resolve(client, ctx, condition["id"])
    assert again.status_code == 200 and again.json()["created"] is False
    conflict = resolve(client, ctx, condition["id"], outcome="not_satisfied")
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "condition_closed"


@pytest.mark.parametrize("role", ["agent", "admin", "voter"])
def test_resolution_requires_the_dedicated_scope(client, ctx, role):
    condition = add_condition(client, ctx, ctx["alice"]).json()["condition"]
    assert resolve(client, ctx, condition["id"], role=role).status_code == 403


def test_other_organizations_cannot_touch_conditions(client, ctx, make_tenant):
    condition = add_condition(client, ctx, ctx["alice"]).json()["condition"]
    other = make_tenant("Globex", resolver=["conditions:resolve"])
    response = client.post(
        f"{API}/requests/{ctx['id']}/conditions/{condition['id']}/resolve",
        json={"outcome": "satisfied", "note": "x"},
        headers=other.headers("resolver"),
    )
    assert response.status_code == 404
    assert (
        client.get(f"{API}/requests/{ctx['id']}/conditions", headers=other.agent).status_code == 404
    )
    response = client.post(
        f"{API}/requests/{ctx['id']}/conditions",
        json={"approver_id": ctx["alice"], "description": "x"},
        headers=other.voter,
    )
    assert response.status_code == 404


def test_agent_cannot_record_conditions(client, ctx):
    response = client.post(
        f"{API}/requests/{ctx['id']}/conditions",
        json={"approver_id": ctx["alice"], "description": "x"},
        headers=ctx["tenant"].agent,
    )
    assert response.status_code == 403


def test_duplicate_and_conflicting_conditions(client, ctx):
    assert add_condition(client, ctx, ctx["alice"]).status_code == 201
    same = add_condition(client, ctx, ctx["alice"])
    assert same.status_code == 200 and same.json()["created"] is False
    different = add_condition(client, ctx, ctx["alice"], "Only on Tuesday")
    assert different.status_code == 409


def test_conditions_on_terminal_requests_are_refused(client, ctx):
    condition = add_condition(client, ctx, ctx["alice"]).json()["condition"]
    vote(client, ctx["tenant"], ctx["id"], ctx["bob"], "reject")
    conditions = client.get(
        f"{API}/requests/{ctx['id']}/conditions", headers=ctx["tenant"].agent
    ).json()
    assert conditions[0]["status"] == "lapsed"
    assert resolve(client, ctx, condition["id"]).status_code == 409
    late = add_condition(client, ctx, ctx["bob"], "Late")
    assert late.status_code == 409


def test_closed_conditions_are_frozen_in_the_database(client, ctx, session):
    condition = add_condition(client, ctx, ctx["alice"]).json()["condition"]
    resolve(client, ctx, condition["id"])
    with pytest.raises(Exception, match="closed"):
        session.execute(text("UPDATE approval_conditions SET status = 'open', closed_at = NULL"))
    session.rollback()
    with pytest.raises(Exception, match="cannot be deleted"):
        session.execute(text("DELETE FROM approval_conditions"))
    session.rollback()


def test_resolution_and_its_audit_roll_back_together(client, ctx, session, monkeypatch):
    condition = add_condition(client, ctx, ctx["alice"]).json()["condition"]
    real = approval_service.audit_service.record_event

    def failing(db, **kwargs):
        if kwargs["event_type"].value == "condition.resolved":
            raise RuntimeError("injected failure")
        return real(db, **kwargs)

    monkeypatch.setattr(approval_service.audit_service, "record_event", failing)
    assert resolve(client, ctx, condition["id"]).status_code == 500
    monkeypatch.setattr(approval_service.audit_service, "record_event", real)
    current = client.get(
        f"{API}/requests/{ctx['id']}/conditions", headers=ctx["tenant"].agent
    ).json()[0]
    assert current["status"] == "open" and current["resolved_by_api_key_id"] is None
    assert "condition.resolved" not in audit_types(client, ctx["tenant"], ctx["id"])


class TestVoiceConditions:
    @pytest.fixture
    def dialled(self, voice_client, make_tenant, run_worker):
        tenant = make_tenant(
            voice=["voice:calls:create", "voice:calls:read"],
            resolver=["conditions:resolve"],
            auditor=["audit:read"],
        )
        alice = create_approver(voice_client, tenant, "Alice", phone_number="+14155550101")
        bob = create_approver(voice_client, tenant, "Bob", phone_number="+14155550102")
        policy = make_policy(voice_client, tenant, [alice["id"], bob["id"]])
        request = create_request(voice_client, tenant, policy["id"])
        schedule_calls(voice_client, tenant, request["id"])
        run_worker()
        calls = list_calls(voice_client, tenant, request["id"])
        return {
            "tenant": tenant,
            "id": request["id"],
            "alice": alice["id"],
            "bob": bob["id"],
            "calls": calls,
        }

    def test_spoken_conditional_approval_becomes_a_condition(
        self, voice_client, dialled, mock_provider, run_worker, session
    ):
        payload = mock_provider.complete(
            dialled["calls"][0]["provider_call_id"],
            decision="conditional_approve",
            condition="only after the database backup is verified",
        )
        deliver_webhook(voice_client, payload, WEBHOOK_TOKEN)
        assert [r for _, r in run_worker()] == ["condition_recorded"]
        assert votes(session) == 0
        call = list_calls(voice_client, dialled["tenant"], dialled["id"])[0]
        assert (call["decision_status"], call["decision_candidate"]) == (
            "conditional",
            "conditional_approve",
        )
        conditions = voice_client.get(
            f"{API}/requests/{dialled['id']}/conditions", headers=dialled["tenant"].agent
        ).json()
        assert conditions[0]["description"] == "only after the database backup is verified"
        assert conditions[0]["source"] == "voice"
        assert conditions[0]["voice_call_id"] == call["id"]
        # Bob then approves unconditionally. Alice's seat holds only a condition, which
        # never counts as approval, so the request stays pending and unauthorized.
        bob = mock_provider.complete(dialled["calls"][1]["provider_call_id"], decision="approve")
        deliver_webhook(voice_client, bob, WEBHOOK_TOKEN)
        run_worker()
        request = voice_client.get(
            f"{API}/requests/{dialled['id']}", headers=dialled["tenant"].agent
        ).json()
        assert request["status"] == "pending"
        assert request["evaluation"]["authorized"] is False

    def test_conditional_answer_without_the_condition_is_no_decision(
        self, voice_client, dialled, mock_provider, run_worker, session
    ):
        payload = mock_provider.complete(
            dialled["calls"][0]["provider_call_id"], decision="conditional_approve"
        )
        deliver_webhook(voice_client, payload, WEBHOOK_TOKEN)
        assert [r for _, r in run_worker()] == ["condition_missing"]
        assert votes(session) == 0

    def test_satisfied_voice_condition_schedules_a_reapproval_call(
        self, voice_client, dialled, mock_provider, run_worker, session
    ):
        payload = mock_provider.complete(
            dialled["calls"][0]["provider_call_id"],
            decision="conditional_approve",
            condition="after QA",
        )
        deliver_webhook(voice_client, payload, WEBHOOK_TOKEN)
        run_worker()
        condition = voice_client.get(
            f"{API}/requests/{dialled['id']}/conditions", headers=dialled["tenant"].agent
        ).json()[0]
        response = voice_client.post(
            f"{API}/requests/{dialled['id']}/conditions/{condition['id']}/resolve",
            json={"outcome": "satisfied", "note": "QA signed off"},
            headers=dialled["tenant"].headers("resolver"),
        )
        assert response.status_code == 200
        session.expire_all()
        alice_calls = session.scalars(
            select(VoiceCall)
            .where(VoiceCall.approver_id == UUID(dialled["alice"]))
            .order_by(VoiceCall.generation)
        ).all()
        assert [c.generation for c in alice_calls] == [1, 2]
        run_worker()
        recall = list_calls(voice_client, dialled["tenant"], dialled["id"])[-1]
        assert recall["generation"] == 2 and recall["status"] == "initiated"
        # Alice approves on the re-approval call; Bob approves too -> approved.
        for call in (recall, dialled["calls"][1]):
            deliver_webhook(
                voice_client, mock_provider.complete(call["provider_call_id"]), WEBHOOK_TOKEN
            )
        run_worker()
        assert get_request(voice_client, dialled["tenant"], dialled["id"])["status"] == "approved"
