import threading
from collections import Counter
from uuid import UUID

import pytest
from alembic import command
from sqlalchemy import func, select, text

from app.db.models import ApprovalRequest, ApprovalVote, AuditEvent
from app.domain.approval_rules import VoteChoice
from app.services import approval_service, audit_service
from app.services.audit_service import AuditEventType
from tests.conftest import alembic_config
from tests.helpers import API, create_approver, create_policy, create_request, vote

pytestmark = pytest.mark.integration


def event_types(session, request_id):
    return [
        event.event_type
        for event in session.scalars(
            select(AuditEvent)
            .where(AuditEvent.request_id == request_id)
            .order_by(AuditEvent.sequence)
        )
    ]


def get(client, tenant, request_id):
    return client.get(f"{API}/requests/{request_id}", headers=tenant.agent).json()


@pytest.fixture
def pending(client, tenant, two_approver_setup):
    request = create_request(client, tenant, two_approver_setup["policy"]["id"])
    return {**two_approver_setup, "request": request, "id": request["id"]}


class TestDecisions:
    def test_one_approver_approves(self, client, tenant, session):
        alice = create_approver(client, tenant, "Alice")
        policy = create_policy(client, tenant, [alice["id"]])
        request = create_request(client, tenant, policy["id"])

        response = vote(client, tenant, request["id"], alice["id"], "approve")
        assert response.status_code == 201
        body = response.json()
        assert body["created"] is True
        assert body["vote"]["vote"] == "approve"
        assert body["request"]["status"] == "approved"
        assert body["request"]["decided_at"] is not None
        assert event_types(session, request["id"]) == [
            "request.created",
            "vote.recorded",
            "request.approved",
        ]

    def test_one_approver_rejects(self, client, tenant):
        alice = create_approver(client, tenant, "Alice")
        policy = create_policy(client, tenant, [alice["id"]])
        request = create_request(client, tenant, policy["id"])
        result = vote(client, tenant, request["id"], alice["id"], "reject").json()
        assert result["request"]["status"] == "rejected"

    def test_first_approval_of_two_stays_pending(self, client, tenant, pending, session):
        result = vote(client, tenant, pending["id"], pending["alice"]["id"], "approve").json()
        assert result["request"] == {"id": pending["id"], "status": "pending", "decided_at": None}
        fetched = get(client, tenant, pending["id"])
        assert fetched["status"] == "pending"
        assert [v["approver_id"] for v in fetched["votes"]] == [pending["alice"]["id"]]
        assert event_types(session, pending["id"]) == ["request.created", "vote.recorded"]

    def test_both_approve(self, client, tenant, pending, session):
        vote(client, tenant, pending["id"], pending["alice"]["id"], "approve")
        result = vote(client, tenant, pending["id"], pending["bob"]["id"], "approve").json()
        assert result["request"]["status"] == "approved"
        fetched = get(client, tenant, pending["id"])
        assert fetched["status"] == "approved"
        assert fetched["decided_at"] == result["request"]["decided_at"]
        # Timestamps are always serialised in UTC, independent of the server timezone.
        assert fetched["decided_at"].endswith("Z") and fetched["created_at"].endswith("Z")
        assert len(fetched["votes"]) == 2
        assert event_types(session, pending["id"]) == [
            "request.created",
            "vote.recorded",
            "vote.recorded",
            "request.approved",
        ]

    def test_approve_then_reject_is_rejected(self, client, tenant, pending, session):
        # The demo scenario: first approver says yes, second says no.
        vote(client, tenant, pending["id"], pending["alice"]["id"], "approve")
        result = vote(client, tenant, pending["id"], pending["bob"]["id"], "reject").json()
        assert result["request"]["status"] == "rejected"
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "request.rejected")
        ).all()
        assert len(events) == 1
        assert events[0].event_data["approve_votes"] == 1
        assert events[0].event_data["reject_votes"] == 1

    def test_first_reject_decides_immediately(self, client, tenant, pending):
        result = vote(client, tenant, pending["id"], pending["alice"]["id"], "reject").json()
        assert result["request"]["status"] == "rejected"


class TestDuplicatesAndConflicts:
    def test_identical_duplicate_is_idempotent(self, client, tenant, pending, session):
        first = vote(client, tenant, pending["id"], pending["alice"]["id"], "approve")
        again = vote(client, tenant, pending["id"], pending["alice"]["id"], "approve")
        assert (first.status_code, again.status_code) == (201, 200)
        assert again.json()["created"] is False
        assert again.json()["vote"] == first.json()["vote"]
        assert len(get(client, tenant, pending["id"])["votes"]) == 1
        assert event_types(session, pending["id"]).count("vote.recorded") == 1

    def test_identical_duplicate_after_decision_returns_outcome(self, client, tenant, pending):
        vote(client, tenant, pending["id"], pending["alice"]["id"], "reject")
        again = vote(client, tenant, pending["id"], pending["alice"]["id"], "reject")
        assert again.status_code == 200
        assert again.json()["request"]["status"] == "rejected"

    def test_conflicting_duplicate_is_409(self, client, tenant, pending):
        vote(client, tenant, pending["id"], pending["alice"]["id"], "approve")
        response = vote(client, tenant, pending["id"], pending["alice"]["id"], "reject")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "vote_conflict"
        assert get(client, tenant, pending["id"])["status"] == "pending"

    @pytest.mark.parametrize("late_vote", ["approve", "reject"])
    def test_vote_after_decision_is_409(self, client, tenant, pending, late_vote, session):
        vote(client, tenant, pending["id"], pending["alice"]["id"], "reject")
        response = vote(client, tenant, pending["id"], pending["bob"]["id"], late_vote)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "request_already_decided"
        fetched = get(client, tenant, pending["id"])
        assert fetched["status"] == "rejected" and len(fetched["votes"]) == 1
        assert event_types(session, pending["id"]).count("vote.recorded") == 1


class TestAuthorization:
    def test_approver_outside_snapshot_cannot_vote(self, client, tenant, pending):
        carol = create_approver(client, tenant, "Carol")
        response = vote(client, tenant, pending["id"], carol["id"], "approve")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "approver_not_authorized"
        assert get(client, tenant, pending["id"])["votes"] == []

    def test_unknown_approver_cannot_vote(self, client, tenant, pending):
        response = vote(
            client, tenant, pending["id"], "00000000-0000-0000-0000-000000000000", "reject"
        )
        assert response.status_code == 400
        assert get(client, tenant, pending["id"])["status"] == "pending"

    def test_deactivated_approver_cannot_vote(self, client, tenant, pending):
        bob = pending["bob"]
        client.patch(
            f"{API}/approvers/{bob['id']}", json={"is_active": False}, headers=tenant.admin
        )
        response = vote(client, tenant, pending["id"], bob["id"], "approve")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "approver_inactive"

    def test_unknown_request_is_404(self, client, tenant, pending):
        response = vote(
            client,
            tenant,
            "00000000-0000-0000-0000-000000000000",
            pending["alice"]["id"],
            "approve",
        )
        assert response.status_code == 404


class TestAtomicity:
    def test_failure_in_decision_rolls_back_vote(
        self, client, tenant, pending, session, monkeypatch
    ):
        vote(client, tenant, pending["id"], pending["alice"]["id"], "approve")
        real_record = audit_service.record_event

        def failing_record(db, **kwargs):
            if kwargs["event_type"] is AuditEventType.REQUEST_APPROVED:
                raise RuntimeError("injected failure")
            return real_record(db, **kwargs)

        monkeypatch.setattr(audit_service, "record_event", failing_record)
        response = vote(client, tenant, pending["id"], pending["bob"]["id"], "approve")
        assert response.status_code == 500

        # Neither Bob's vote, its audit event, nor the status change was persisted.
        fetched = get(client, tenant, pending["id"])
        assert fetched["status"] == "pending" and fetched["decided_at"] is None
        assert [v["approver_id"] for v in fetched["votes"]] == [pending["alice"]["id"]]
        assert event_types(session, pending["id"]) == ["request.created", "vote.recorded"]

        # Once the fault is gone the same vote succeeds normally.
        monkeypatch.setattr(audit_service, "record_event", real_record)
        result = vote(client, tenant, pending["id"], pending["bob"]["id"], "approve")
        assert result.status_code == 201
        assert result.json()["request"]["status"] == "approved"

    def test_database_rejects_inconsistent_decision_state(self, pending, session):
        with pytest.raises(Exception, match="decided_at_matches_status"):
            session.execute(
                text("UPDATE approval_requests SET status = 'approved' WHERE id = :id"),
                {"id": pending["id"]},
            )
        session.rollback()

    def test_database_rejects_duplicate_vote_rows(self, client, tenant, pending, session):
        vote(client, tenant, pending["id"], pending["alice"]["id"], "approve")
        existing = session.scalars(select(ApprovalVote)).one()
        session.add(
            ApprovalVote(
                organization_id=existing.organization_id,
                request_id=existing.request_id,
                approver_id=existing.approver_id,
                vote="reject",
            )
        )
        with pytest.raises(Exception, match="uq_approval_votes_request_id_approver_id"):
            session.flush()
        session.rollback()


class TestConcurrency:
    """Real PostgreSQL row locking: each thread uses its own session and connection."""

    def _race(self, session_factory, tenant, request_id, ballots):
        barrier = threading.Barrier(len(ballots))
        results: list[object] = []
        lock = threading.Lock()

        def cast(approver_id, choice):
            with session_factory() as db:
                barrier.wait()
                try:
                    outcome = approval_service.cast_vote(
                        db, tenant.organization_id, request_id, approver_id, choice
                    )
                    result: object = ("ok", outcome.created, outcome.request.status)
                except Exception as exc:  # collected and asserted below
                    result = ("error", getattr(exc, "code", type(exc).__name__))
                with lock:
                    results.append(result)

        threads = [threading.Thread(target=cast, args=ballot) for ballot in ballots]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert len(results) == len(ballots)
        return results

    def _counts(self, session, request_id):
        session.expire_all()
        votes = session.scalar(
            select(func.count())
            .select_from(ApprovalVote)
            .where(ApprovalVote.request_id == request_id)
        )
        events = Counter(
            session.scalars(
                select(AuditEvent.event_type).where(AuditEvent.request_id == request_id)
            )
        )
        status = session.scalar(
            select(ApprovalRequest.status).where(ApprovalRequest.id == request_id)
        )
        return votes, events, status

    @pytest.mark.parametrize("attempt", range(3))
    def test_concurrent_approvals_decide_exactly_once(
        self, session_factory, session, tenant, pending, attempt
    ):
        request_id = UUID(pending["id"])
        ballots = [
            (UUID(pending["alice"]["id"]), VoteChoice.APPROVE),
            (UUID(pending["bob"]["id"]), VoteChoice.APPROVE),
        ]
        results = self._race(session_factory, tenant, request_id, ballots)
        assert sorted(r[0] for r in results) == ["ok", "ok"]
        votes, events, status = self._counts(session, request_id)
        assert (votes, status) == (2, "approved")
        assert events["vote.recorded"] == 2 and events["request.approved"] == 1

    @pytest.mark.parametrize("attempt", range(3))
    def test_concurrent_duplicate_votes_record_once(
        self, session_factory, session, tenant, pending, attempt
    ):
        request_id = UUID(pending["id"])
        alice = UUID(pending["alice"]["id"])
        results = self._race(session_factory, tenant, request_id, [(alice, VoteChoice.APPROVE)] * 4)
        assert all(r[0] == "ok" for r in results)
        assert sorted(r[1] for r in results) == [False, False, False, True]
        votes, events, status = self._counts(session, request_id)
        assert (votes, status, events["vote.recorded"]) == (1, "pending", 1)

    def test_concurrent_approve_and_reject_never_approves(
        self, session_factory, session, tenant, pending
    ):
        request_id = UUID(pending["id"])
        ballots = [
            (UUID(pending["alice"]["id"]), VoteChoice.APPROVE),
            (UUID(pending["bob"]["id"]), VoteChoice.REJECT),
        ]
        self._race(session_factory, tenant, request_id, ballots)
        _, events, status = self._counts(session, request_id)
        assert status == "rejected"
        assert events["request.approved"] == 0 and events["request.rejected"] == 1


def test_migrations_downgrade_and_upgrade_cleanly(test_settings, engine):
    config = alembic_config(test_settings.database_url)
    command.downgrade(config, "base")
    with engine.connect() as connection:
        remaining = connection.execute(
            text(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' "
                "AND table_name <> 'alembic_version'"
            )
        ).scalar_one()
    assert remaining == 0
    command.upgrade(config, "head")
