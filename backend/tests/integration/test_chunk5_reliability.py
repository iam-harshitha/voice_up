"""Chunk 5 failure injection against the real application (mock voice only).

Each scenario drives the actual FastAPI app, PostgreSQL test database, worker and policy
engine. Faults are injected only at the transport boundary (a lost HTTP response) or
through the existing simulated phone answers; nothing writes decisions directly.
"""

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import func, select

from app.db.models import ApprovalRequest, ApprovalVote, AuditEvent
from approval_line_agents.client import ApprovalLineClient, ClientSettings
from approval_line_agents.demo import (
    ACTION,
    EXIT_EXECUTED,
    EXIT_EXPIRED,
    EXIT_GATE_REFUSED,
    EXIT_REJECTED,
    EXIT_TIMEOUT,
    SUMMARY,
    Clock,
    RestGateway,
    SimulatedDeployment,
    WaitConfig,
    run_agent,
)
from tests.conftest import WEBHOOK_TOKEN
from tests.helpers import (
    API,
    create_approver,
    deliver_webhook,
    get_request,
    list_calls,
    make_policy,
    schedule_calls,
)

pytestmark = pytest.mark.integration

RELEASE = "v2.4"


class LoseFirstResponse(httpx.AsyncBaseTransport):
    """Forwards every request to the real app, but drops the response of the first
    POST /requests after the server has fully processed it (an ambiguous timeout)."""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self.inner = inner
        self.posts = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        if request.method == "POST" and request.url.path.endswith("/requests"):
            self.posts += 1
            if self.posts == 1:
                await response.aread()  # the server committed; only the reply is lost
                raise httpx.ReadTimeout("response lost in transit", request=request)
        return response


class FakeTime:
    def __init__(self, steps):
        self.now = 0.0
        self.steps = list(steps)

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        if self.steps:
            await asyncio.to_thread(self.steps.pop(0))
        self.now += seconds


@pytest.fixture
def world(voice_client, make_tenant, run_worker, mock_provider, tmp_path):
    tenant = make_tenant(voice=["voice:calls:create", "voice:calls:read"])
    alice = create_approver(voice_client, tenant, "Alice", phone_number="+14155550181")["id"]
    bob = create_approver(voice_client, tenant, "Bob", phone_number="+14155550182")["id"]
    policy = make_policy(voice_client, tenant, [alice, bob], decision_deadline_seconds=3600)

    def answer(request_id, approver, decision):
        def step():
            calls = list_calls(voice_client, tenant, request_id)
            call = next(c for c in calls if c["approver_id"] == approver)
            payload = mock_provider.complete(call["provider_call_id"], decision=decision)
            deliver_webhook(voice_client, payload, WEBHOOK_TOKEN).raise_for_status()
            run_worker()

        return step

    return {
        "client": voice_client,
        "tenant": tenant,
        "alice": alice,
        "bob": bob,
        "policy": policy,
        "answer": answer,
        "run_worker": run_worker,
        "records": tmp_path / "runs",
    }


def api_client(world, transport):
    return ApprovalLineClient(
        ClientSettings("http://approval-line.test", world["tenant"].keys["agent"], 10),
        transport=transport,
    )


def count(session, model, *where):
    session.expire_all()
    return session.scalar(select(func.count()).select_from(model).where(*where))


def test_lost_creation_response_is_retried_without_a_duplicate_and_rejection_halts(world, session):
    """The first POST's reply is lost after the server committed. The agent's client
    retries with the same Idempotency-Key and receives the SAME request; a phone
    rejection then halts the agent with nothing executed."""
    transport = LoseFirstResponse(httpx.ASGITransport(app=world["client"].app))
    state: dict[str, str] = {}

    def schedule():
        rid = (
            world["client"]
            .get(f"{API}/requests", headers=world["tenant"].agent)
            .json()["items"][0]["id"]
        )
        state["id"] = rid
        schedule_calls(world["client"], world["tenant"], rid)
        world["run_worker"]()

    async def go():
        lines: list[str] = []
        time = FakeTime(
            [schedule, lambda: world["answer"](state["id"], world["alice"], "reject")()]
        )
        executor = SimulatedDeployment(world["records"])
        async with api_client(world, transport) as api:
            code = await run_agent(
                RestGateway(api),
                policy_id=uuid.UUID(world["policy"]["id"]),
                release=RELEASE,
                executor=executor,
                config=WaitConfig(poll_interval=1, max_interval=2, timeout=60),
                report=lines.append,
                clock=Clock(monotonic=time.monotonic, sleep=time.sleep),
            )
        return code, executor, lines

    code, executor, lines = asyncio.run(go())
    assert transport.posts == 2  # the original attempt plus one keyed retry
    assert count(session, ApprovalRequest) == 1  # no duplicate request
    assert count(session, AuditEvent, AuditEvent.event_type == "request.created") == 1
    created = next(line for line in lines if line.startswith("Approval request created:"))
    assert created.endswith(state["id"])
    assert code == EXIT_REJECTED and executor.executions == []


def test_lost_response_without_a_key_is_not_retried(world, session):
    transport = LoseFirstResponse(httpx.ASGITransport(app=world["client"].app))

    async def go():
        async with api_client(world, transport) as api:
            await api.create_request(
                uuid.UUID(world["policy"]["id"]), ACTION, SUMMARY.format(release=RELEASE), {}
            )

    with pytest.raises(Exception, match="did not respond in time"):
        asyncio.run(go())
    assert transport.posts == 1
    # The server did create it: this is exactly why keyless creation is never retried.
    assert count(session, ApprovalRequest) == 1


def test_a_completed_call_without_an_explicit_decision_never_authorizes(world, session):
    client, tenant = world["client"], world["tenant"]
    created = client.post(
        f"{API}/requests",
        json={
            "policy_id": world["policy"]["id"],
            "action": ACTION,
            "summary": SUMMARY.format(release=RELEASE),
        },
        headers=tenant.agent,
    ).json()
    schedule_calls(client, tenant, created["id"])
    world["run_worker"]()

    async def go():
        time = FakeTime(
            [
                world["answer"](created["id"], world["alice"], "approve"),
                world["answer"](created["id"], world["bob"], None),  # call ends, no decision
            ]
        )
        executor = SimulatedDeployment(world["records"])
        async with api_client(world, httpx.ASGITransport(app=client.app)) as api:
            code = await run_agent(
                RestGateway(api),
                policy_id=uuid.UUID(world["policy"]["id"]),
                release=RELEASE,
                executor=executor,
                config=WaitConfig(poll_interval=1, max_interval=2, timeout=20),
                report=lambda _line: None,
                resume_request_id=uuid.UUID(created["id"]),
                clock=Clock(monotonic=time.monotonic, sleep=time.sleep),
            )
        return code, executor

    code, executor = asyncio.run(go())
    assert code == EXIT_TIMEOUT and executor.executions == []
    bob_call = next(
        c for c in list_calls(client, tenant, created["id"]) if c["approver_id"] == world["bob"]
    )
    assert bob_call["status"] == "completed" and bob_call["decision_status"] == "no_decision"
    assert count(session, ApprovalVote) == 1  # only Alice's explicit approval
    detail = get_request(client, tenant, created["id"])
    assert detail["status"] == "pending" and detail["evaluation"]["authorized"] is False


def test_replayed_webhook_after_a_decision_changes_nothing(world, session, mock_provider):
    client, tenant = world["client"], world["tenant"]
    created = client.post(
        f"{API}/requests",
        json={"policy_id": world["policy"]["id"], "action": ACTION, "summary": "s"},
        headers=tenant.agent,
    ).json()
    schedule_calls(client, tenant, created["id"])
    world["run_worker"]()
    call = next(
        c for c in list_calls(client, tenant, created["id"]) if c["approver_id"] == world["alice"]
    )
    payload = mock_provider.complete(call["provider_call_id"], decision="approve")
    for _ in range(3):  # Bolna retries the same delivery
        assert deliver_webhook(client, payload, WEBHOOK_TOKEN).status_code == 200
        world["run_worker"]()
    assert count(session, ApprovalVote) == 1
    assert deliver_webhook(client, payload, "x" * 40).status_code == 401  # wrong token
    assert count(session, ApprovalVote) == 1


# --- F1: an approval observed after the deadline (real backend) -------------------------


def _late_clock(monkeypatch, seconds: int):
    """Advance only the evaluation clock: the request was approved earlier, now is later."""
    from datetime import timedelta

    from app.db.base import utcnow
    from app.services import approval_service

    monkeypatch.setattr(approval_service, "utcnow", lambda: utcnow() + timedelta(seconds=seconds))


def _resume_agent(world, request_id, timeout=10):
    async def go():
        executor = SimulatedDeployment(world["records"])
        async with api_client(world, httpx.ASGITransport(app=world["client"].app)) as api:
            code = await run_agent(
                RestGateway(api),
                policy_id=uuid.UUID(world["policy"]["id"]),
                release=RELEASE,
                executor=executor,
                config=WaitConfig(poll_interval=1, max_interval=1, timeout=timeout),
                report=lambda _line: None,
                resume_request_id=uuid.UUID(request_id),
                clock=Clock(monotonic=FakeTime([]).monotonic, sleep=FakeTime([]).sleep),
            )
        return code, executor

    return asyncio.run(go())


def test_approval_observed_after_its_deadline_executes_once(world, monkeypatch):
    client, tenant = world["client"], world["tenant"]
    created = client.post(
        f"{API}/requests",
        json={
            "policy_id": world["policy"]["id"],
            "action": ACTION,
            "summary": SUMMARY.format(release=RELEASE),
        },
        headers=tenant.agent,
    ).json()
    for approver in (world["alice"], world["bob"]):
        client.post(
            f"{API}/requests/{created['id']}/votes",
            json={"approver_id": approver, "vote": "approve"},
            headers=tenant.voter,
        ).raise_for_status()
    _late_clock(monkeypatch, 3600 + 120)  # the agent looks two minutes after the deadline
    detail = get_request(client, tenant, created["id"])
    # The backend genuinely reports both: approved before the deadline, clock now past it.
    assert detail["status"] == "approved" and detail["evaluation"]["authorized"] is True
    assert detail["evaluation"]["deadline_passed"] is True
    assert detail["decided_at"] < detail["expires_at"]

    code, executor = _resume_agent(world, created["id"])
    assert code == EXIT_EXECUTED and len(executor.executions) == 1
    code_again, _ = _resume_agent(world, created["id"])  # restart: no second execution
    assert code_again == EXIT_GATE_REFUSED and len(executor.executions) == 1


def test_undecided_request_past_its_deadline_never_executes(world, monkeypatch):
    client, tenant = world["client"], world["tenant"]
    created = client.post(
        f"{API}/requests",
        json={
            "policy_id": world["policy"]["id"],
            "action": ACTION,
            "summary": SUMMARY.format(release=RELEASE),
        },
        headers=tenant.agent,
    ).json()
    client.post(
        f"{API}/requests/{created['id']}/votes",
        json={"approver_id": world["alice"], "vote": "approve"},
        headers=tenant.voter,
    ).raise_for_status()
    _late_clock(monkeypatch, 3600 + 120)
    # A late vote is refused by the backend, so the request can never become approved.
    late = client.post(
        f"{API}/requests/{created['id']}/votes",
        json={"approver_id": world["bob"], "vote": "approve"},
        headers=tenant.voter,
    )
    assert late.status_code == 409 and late.json()["error"]["code"] == "request_expired"
    code, executor = _resume_agent(world, created["id"])
    assert code == EXIT_EXPIRED and executor.executions == []
