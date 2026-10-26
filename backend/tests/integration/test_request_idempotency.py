"""Idempotency-Key on POST /requests: retries never create duplicate requests."""

import threading
from uuid import UUID

import pytest
from sqlalchemy import func, select, text

from app.db.models import ApprovalRequest, AuditEvent
from app.schemas.request import ApprovalRequestCreate
from app.services import approval_service
from tests.helpers import API, create_approver, make_policy

pytestmark = pytest.mark.integration

KEY = "deploy-v2.4-7f3c9a1e"


@pytest.fixture
def policy(client, tenant):
    a = create_approver(client, tenant, "Alice")["id"]
    return make_policy(client, tenant, [a])


def body(policy, **overrides):
    return {
        "policy_id": policy["id"],
        "action": "deploy_to_production",
        "summary": "Deploy release v2.4",
        "context": {"release": "v2.4"},
        **overrides,
    }


def post(client, tenant, payload, key=KEY):
    headers = dict(tenant.agent)
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post(f"{API}/requests", json=payload, headers=headers)


def count_requests(session) -> int:
    session.expire_all()
    return session.scalar(select(func.count()).select_from(ApprovalRequest))


def test_retry_with_the_same_key_replays_the_original(client, tenant, policy, session):
    first = post(client, tenant, body(policy))
    assert first.status_code == 201 and "Idempotent-Replayed" not in first.headers
    second = post(client, tenant, body(policy))
    assert second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["evaluation"] == first.json()["evaluation"]
    assert count_requests(session) == 1
    created_events = session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.event_type == "request.created")
    )
    assert created_events == 1


def test_replay_reflects_current_state_not_a_cached_response(client, tenant, policy):
    first = post(client, tenant, body(policy)).json()
    approver = first["authorized_approver_ids"][0]
    client.post(
        f"{API}/requests/{first['id']}/votes",
        json={"approver_id": approver, "vote": "reject"},
        headers=tenant.voter,
    ).raise_for_status()
    replay = post(client, tenant, body(policy)).json()
    assert replay["id"] == first["id"] and replay["status"] == "rejected"
    assert replay["evaluation"]["authorized"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"action": "drop_database"},
        {"summary": "Something else"},
        {"context": {"release": "v9.9"}},
    ],
)
def test_same_key_with_a_different_payload_is_refused(client, tenant, policy, session, change):
    post(client, tenant, body(policy)).raise_for_status()
    response = post(client, tenant, body(policy, **change))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_reused"
    assert count_requests(session) == 1


def test_keys_are_scoped_to_the_organization(client, tenant, policy, make_tenant, session):
    other = make_tenant("Globex")
    b = create_approver(client, other, "Bob")["id"]
    other_policy = make_policy(client, other, [b])
    mine = post(client, tenant, body(policy))
    theirs = post(client, other, body(other_policy))
    assert mine.status_code == theirs.status_code == 201
    assert mine.json()["id"] != theirs.json()["id"]
    assert count_requests(session) == 2


def test_without_a_key_behaviour_is_unchanged(client, tenant, policy, session):
    assert post(client, tenant, body(policy), key=None).status_code == 201
    assert post(client, tenant, body(policy), key=None).status_code == 201
    assert count_requests(session) == 2


@pytest.mark.parametrize(
    "key",
    ["short", "x" * 129, "has space here", "semi;colon!", "key/with/slash", "ünïcode-key"],
)
def test_malformed_keys_are_rejected_before_anything_is_written(
    client, tenant, policy, session, key
):
    # Sent as raw Latin-1 bytes so non-ASCII keys reach the server's validation.
    headers = {**tenant.agent, "Idempotency-Key": key.encode("latin-1")}
    response = client.post(f"{API}/requests", json=body(policy), headers=headers)
    assert response.status_code == 422
    assert count_requests(session) == 0


def test_the_key_is_stored_only_as_a_hash(client, tenant, policy, session):
    post(client, tenant, body(policy)).raise_for_status()
    row = session.execute(
        text("SELECT idempotency_key_hash, idempotency_fingerprint FROM approval_requests")
    ).one()
    assert KEY not in row[0] and len(row[0]) == 64 and len(row[1]) == 64
    assert "v2.4" not in row[1]


def test_failed_creation_does_not_consume_the_key(client, tenant, policy, session):
    client.patch(
        f"{API}/policies/{policy['id']}", json={"is_active": False}, headers=tenant.admin
    ).raise_for_status()
    refused = post(client, tenant, body(policy))
    assert refused.status_code == 400 and count_requests(session) == 0
    client.patch(
        f"{API}/policies/{policy['id']}", json={"is_active": True}, headers=tenant.admin
    ).raise_for_status()
    assert post(client, tenant, body(policy)).status_code == 201


@pytest.mark.parametrize("attempt", range(3))
def test_concurrent_retries_with_one_key_create_exactly_one_request(
    tenant, policy, session_factory, session, attempt
):
    data = ApprovalRequestCreate.model_validate(body(policy))
    workers = 8
    barrier = threading.Barrier(workers)
    results: list[tuple[UUID, bool] | str] = []
    lock = threading.Lock()

    def submit():
        with session_factory() as db:
            barrier.wait()
            try:
                request, created = approval_service.submit_request(
                    db, tenant.organization_id, data, idempotency_key=f"race-{attempt}-key"
                )
                outcome: tuple[UUID, bool] | str = (request.id, created)
            except Exception as exc:  # recorded and asserted below
                outcome = type(exc).__name__
            with lock:
                results.append(outcome)

    threads = [threading.Thread(target=submit) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert all(isinstance(r, tuple) for r in results), results
    ids = {r[0] for r in results}
    assert len(ids) == 1
    assert sum(1 for r in results if r[1]) == 1  # exactly one creator, the rest replays
    assert count_requests(session) == 1
    # The losers rolled back completely: one snapshot, one creation event.
    assert session.scalar(text("SELECT count(*) FROM request_approvers")) == 1
    assert (
        session.scalar(
            text("SELECT count(*) FROM audit_events WHERE event_type = 'request.created'")
        )
        == 1
    )


def test_database_requires_key_hash_and_fingerprint_together(client, tenant, policy, session):
    request_id = post(client, tenant, body(policy), key=None).json()["id"]
    with pytest.raises(Exception, match="idempotency_fields_together"):
        session.execute(
            text("UPDATE approval_requests SET idempotency_key_hash = 'x' WHERE id = :r"),
            {"r": request_id},
        )
    session.rollback()
