"""Consensus through the real vote path (API -> approval_service -> PostgreSQL)."""

import threading
from uuid import UUID

import pytest
from sqlalchemy import func, select, text

from app.db.models import ApprovalVote, AuditEvent
from app.domain.approval_rules import VoteChoice
from app.services import approval_service
from tests.helpers import create_approver, create_request, get_request, make_policy, vote

pytestmark = pytest.mark.integration


@pytest.fixture
def trio(client, tenant):
    return [create_approver(client, tenant, n)["id"] for n in ("Ann", "Ben", "Cat")]


def quorum_request(client, tenant, trio, **fields):
    policy = make_policy(
        client, tenant, trio, approval_mode="quorum", required_approvals=2, **fields
    )
    return create_request(client, tenant, policy["id"])


def final_events(session, request_id):
    session.expire_all()
    return session.scalars(
        select(AuditEvent.event_type).where(
            AuditEvent.request_id == UUID(request_id),
            AuditEvent.event_type.in_(("request.approved", "request.rejected", "request.expired")),
        )
    ).all()


def test_two_of_three_approves_on_second_approval(client, tenant, trio, session):
    request = quorum_request(client, tenant, trio)
    first = vote(client, tenant, request["id"], trio[0], "approve").json()
    assert first["request"]["status"] == "pending"
    evaluation = get_request(client, tenant, request["id"])["evaluation"]
    assert (evaluation["approvals"], evaluation["required_approvals"]) == (1, 2)
    assert evaluation["outstanding_approver_ids"] == trio[1:]
    second = vote(client, tenant, request["id"], trio[2], "approve").json()
    assert second["request"]["status"] == "approved"
    detail = get_request(client, tenant, request["id"])
    assert detail["evaluation"]["authorized"] is True
    assert detail["evaluation"]["reason"] == "quorum_met"
    # The third approver's late vote cannot reopen or change anything.
    late = vote(client, tenant, request["id"], trio[1], "reject")
    assert late.status_code == 409
    assert final_events(session, request["id"]) == ["request.approved"]


def test_any_rejection_vetoes_quorum_policy(client, tenant, trio):
    request = quorum_request(client, tenant, trio)
    vote(client, tenant, request["id"], trio[0], "approve")
    result = vote(client, tenant, request["id"], trio[1], "reject").json()
    assert result["request"]["status"] == "rejected"
    assert get_request(client, tenant, request["id"])["evaluation"]["veto_triggered"] is True


def test_designated_veto_holder_vs_ordinary_rejection(client, tenant, trio):
    request = quorum_request(
        client, tenant, trio, veto_mode="designated", veto_approver_ids=[trio[0]]
    )
    # An ordinary approver's rejection only removes their seat from the quorum.
    assert (
        vote(client, tenant, request["id"], trio[1], "reject").json()["request"]["status"]
        == "pending"
    )
    # The veto holder's rejection rejects immediately.
    result = vote(client, tenant, request["id"], trio[0], "reject").json()
    assert result["request"]["status"] == "rejected"
    evaluation = get_request(client, tenant, request["id"])["evaluation"]
    assert (evaluation["reason"], evaluation["veto_triggered"]) == ("veto", True)


def test_no_veto_mode_rejects_when_quorum_becomes_unreachable(client, tenant, trio):
    request = quorum_request(client, tenant, trio, veto_mode="none")
    assert (
        vote(client, tenant, request["id"], trio[0], "reject").json()["request"]["status"]
        == "pending"
    )
    result = vote(client, tenant, request["id"], trio[1], "reject").json()
    assert result["request"]["status"] == "rejected"
    evaluation = get_request(client, tenant, request["id"])["evaluation"]
    assert (evaluation["reason"], evaluation["veto_triggered"]) == ("quorum_unreachable", False)


def test_unanimous_chunk1_policy_unchanged(client, tenant, trio):
    policy = make_policy(client, tenant, trio[:2])
    request = create_request(client, tenant, policy["id"])
    vote(client, tenant, request["id"], trio[0], "approve")
    assert get_request(client, tenant, request["id"])["status"] == "pending"
    vote(client, tenant, request["id"], trio[1], "approve")
    assert get_request(client, tenant, request["id"])["status"] == "approved"


def test_database_refuses_votes_from_outside_the_snapshot(client, tenant, trio, session):
    policy = make_policy(client, tenant, trio[:2])
    request = create_request(client, tenant, policy["id"])
    org = session.scalar(
        text("SELECT organization_id FROM approval_requests WHERE id=:i"), {"i": request["id"]}
    )
    with pytest.raises(Exception, match="request_approvers"):
        session.execute(
            text(
                "INSERT INTO approval_votes (id, organization_id, request_id, approver_id,"
                " seat_approver_id, vote, created_at) VALUES (gen_random_uuid(), :o, :r, :a,"
                " :a, 'approve', now())"
            ),
            {"o": org, "r": request["id"], "a": trio[2]},
        )
    session.rollback()


def test_request_approvers_rows_are_immutable(client, tenant, trio, session):
    policy = make_policy(client, tenant, trio[:2])
    create_request(client, tenant, policy["id"])
    for statement in (
        "UPDATE request_approvers SET can_veto = true",
        "DELETE FROM request_approvers",
    ):
        with pytest.raises(Exception, match="immutable"):
            session.execute(text(statement))
        session.rollback()


@pytest.mark.parametrize("attempt", range(3))
def test_concurrent_distinct_votes_on_quorum_policy(
    client, tenant, trio, session_factory, session, attempt
):
    request = quorum_request(client, tenant, trio)
    barrier = threading.Barrier(3)
    outcomes: list[str] = []

    def cast(approver):
        with session_factory() as db:
            barrier.wait()
            try:
                approval_service.cast_vote(
                    db,
                    tenant.organization_id,
                    UUID(request["id"]),
                    UUID(approver),
                    VoteChoice.APPROVE,
                )
                outcomes.append("ok")
            except Exception as exc:  # collected and asserted below
                outcomes.append(getattr(exc, "code", type(exc).__name__))

    threads = [threading.Thread(target=cast, args=(a,)) for a in trio]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    # Exactly two votes can land: the third arrives after approval and is refused.
    assert sorted(outcomes) == ["ok", "ok", "request_already_decided"]
    session.expire_all()
    votes = session.scalar(
        select(func.count())
        .select_from(ApprovalVote)
        .where(ApprovalVote.request_id == UUID(request["id"]))
    )
    assert votes == 2
    assert final_events(session, request["id"]) == ["request.approved"]
