"""Chunk 4 end-to-end, MOCK-BACKED (no real phone calls).

The real FastAPI application, PostgreSQL test database, durable job worker, mock voice
provider and token-authenticated webhook flow decide the request; the MCP server, the
REST client, the dashboard read endpoints and the demo agent all observe the SAME
persisted request. Human decisions are simulated only through the existing trusted
voice path (mock provider execution + verified webhook + worker); no vote is inserted
directly and nothing bypasses the approval service.
"""

import asyncio
import json
import uuid
from collections.abc import Callable

import httpx
import pytest
from mcp import Client

from approval_line_agents.client import ApprovalLineClient, ClientSettings
from approval_line_agents.demo import (
    ACTION,
    EXIT_EXECUTED,
    EXIT_GATE_REFUSED,
    EXIT_REJECTED,
    EXIT_TIMEOUT,
    SUMMARY,
    Clock,
    McpGateway,
    RestGateway,
    SimulatedDeployment,
    WaitConfig,
    run_agent,
)
from approval_line_agents.mcp_server import build_server
from tests.conftest import WEBHOOK_TOKEN
from tests.helpers import (
    API,
    create_approver,
    create_dashboard_user,
    dashboard_login,
    deliver_webhook,
    get_request,
    list_calls,
    make_policy,
    schedule_calls,
)

pytestmark = pytest.mark.integration

RELEASE = "v2.4"


class FakeTime:
    """Agent time: each wait runs the next scripted human step, then advances the clock."""

    def __init__(self, steps: list[Callable[[], None]]):
        self.now = 0.0
        self.steps = list(steps)

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        if self.steps:
            await asyncio.to_thread(self.steps.pop(0))
        self.now += seconds


@pytest.fixture
def world(voice_client, make_tenant, run_worker, mock_provider, tmp_path):
    tenant = make_tenant(
        voice=["voice:calls:create", "voice:calls:read"],
        resolver=["conditions:resolve"],
        mcp=["requests:create", "requests:read", "audit:read", "policies:read"],
    )
    alice = create_approver(voice_client, tenant, "Alice", phone_number="+14155550171")["id"]
    bob = create_approver(voice_client, tenant, "Bob", phone_number="+14155550172")["id"]
    policy = make_policy(voice_client, tenant, [alice, bob], decision_deadline_seconds=3600)

    def api_client() -> ApprovalLineClient:
        return ApprovalLineClient(
            ClientSettings("http://approval-line.test", tenant.keys["mcp"], 10),
            transport=httpx.ASGITransport(app=voice_client.app),
        )

    def call_for(request_id: str, approver: str, *, generation: int | None = None) -> dict:
        calls = [
            c for c in list_calls(voice_client, tenant, request_id) if c["approver_id"] == approver
        ]
        return (
            calls[-1]
            if generation is None
            else next(c for c in calls if c["generation"] == generation)
        )

    def human(request_id: str, approver: str, decision: str, condition: str | None = None):
        """An approver answers their (mock) phone call; the result goes through the real
        webhook endpoint and worker."""

        def step() -> None:
            call = call_for(request_id, approver)
            payload = mock_provider.complete(
                call["provider_call_id"], decision=decision, condition=condition
            )
            deliver_webhook(voice_client, payload, WEBHOOK_TOKEN).raise_for_status()
            run_worker()

        return step

    return {
        "client": voice_client,
        "tenant": tenant,
        "alice": alice,
        "bob": bob,
        "policy": policy,
        "api_client": api_client,
        "human": human,
        "run_worker": run_worker,
        "records": tmp_path / "runs",
    }


def start_calls(world, request_id: str) -> None:
    schedule_calls(world["client"], world["tenant"], request_id)
    world["run_worker"]()  # the worker dials both approvers (simulated)


async def with_mcp(world, body):
    async with world["api_client"]() as api, Client(build_server(api)) as session:
        return await body(session)


def created_request_id(lines: list[str]) -> str:
    line = next(line for line in lines if line.startswith("Approval request created:"))
    return line.split(":", 1)[1].strip()


def test_flagship_flow_mcp_agent_waits_through_condition_and_reapproval(world):
    """Agent asks via MCP; Alice approves, Bob sets a condition; the condition is resolved
    and Bob re-approves by phone; only then does the simulated deployment run, once."""
    client, tenant = world["client"], world["tenant"]
    state: dict[str, str] = {}

    def schedule() -> None:
        listed = client.get(f"{API}/requests?status=pending", headers=tenant.agent).json()
        state["id"] = listed["items"][0]["id"]
        start_calls(world, state["id"])

    def alice_approves() -> None:
        world["human"](state["id"], world["alice"], "approve")()

    def bob_sets_condition() -> None:
        world["human"](
            state["id"],
            world["bob"],
            "conditional_approve",
            "only after the database backup is verified",
        )()

    def check_still_blocked() -> None:
        detail = get_request(client, tenant, state["id"])
        assert detail["status"] == "pending" and detail["evaluation"]["authorized"] is False
        assert detail["evaluation"]["reason"] == "condition_open"

    def resolve_condition() -> None:
        [condition] = client.get(
            f"{API}/requests/{state['id']}/conditions", headers=tenant.agent
        ).json()
        client.post(
            f"{API}/requests/{state['id']}/conditions/{condition['id']}/resolve",
            json={"outcome": "satisfied", "note": "backup verified", "evidence_reference": "CHG-1"},
            headers=tenant.headers("resolver"),
        ).raise_for_status()
        detail = get_request(client, tenant, state["id"])
        # Satisfied is not approval: still blocked on Bob's re-approval.
        assert detail["evaluation"]["reason"] == "reapproval_required"
        assert detail["evaluation"]["authorized"] is False
        world["run_worker"]()  # dials Bob again (generation 2)

    def bob_reapproves() -> None:
        world["human"](state["id"], world["bob"], "approve")()

    steps = [
        schedule,
        alice_approves,
        bob_sets_condition,
        check_still_blocked,
        resolve_condition,
        bob_reapproves,
    ]

    async def run_with_mcp(session):
        lines: list[str] = []
        time = FakeTime(steps)
        executor = SimulatedDeployment(world["records"])
        code = await run_agent(
            McpGateway(session),
            policy_id=uuid.UUID(world["policy"]["id"]),
            release=RELEASE,
            executor=executor,
            config=WaitConfig(poll_interval=1, max_interval=2, timeout=120),
            report=lines.append,
            clock=Clock(monotonic=time.monotonic, sleep=time.sleep),
        )
        return code, lines, executor

    code, lines, executor = asyncio.run(with_mcp(world, run_with_mcp))
    assert code == EXIT_EXECUTED, "\n".join(lines)
    assert len(executor.executions) == 1
    request_id = created_request_id(lines)
    assert request_id == state["id"]
    record = json.loads(executor.executions[0].read_text())
    assert record["authorized_by_request_id"] == request_id and record["simulated"] is True
    # The agent saw the blocked states on the way.
    assert any("reason=condition_open" in line for line in lines)
    assert any("reason=reapproval_required" in line for line in lines)

    # REST (dashboard read model) shows the same final state.
    detail = get_request(client, tenant, request_id)
    assert detail["status"] == "approved" and detail["evaluation"]["authorized"] is True
    assert [(a["name"], a["role"]) for a in detail["approvers"]] == [
        ("Alice", "primary"),
        ("Bob", "primary"),
    ]
    assert {v["approver_id"] for v in detail["votes"]} == {world["alice"], world["bob"]}

    # The MCP timeline tool returns the persisted audit history of the same request.
    async def timeline(session):
        result = await session.call_tool("get_approval_timeline", {"request_id": request_id})
        return result.structured_content

    events = asyncio.run(with_mcp(world, timeline))["items"]
    types = [e["event_type"] for e in events]
    for expected in (
        "request.created",
        "voice.call_scheduled",
        "condition.created",
        "condition.resolved",
        "condition.reapproval_requested",
        "request.approved",
    ):
        assert expected in types
    assert types.count("vote.recorded") == 2
    for secret in (WEBHOOK_TOKEN, "+14155550171", "+14155550172", tenant.keys["mcp"]):
        assert secret not in json.dumps(events)

    # A second agent run watching the same approved request over REST does not deploy again.
    async def rest_resume():
        async with world["api_client"]() as api:
            return await run_agent(
                RestGateway(api),
                policy_id=uuid.UUID(world["policy"]["id"]),
                release=RELEASE,
                executor=executor,
                config=WaitConfig(poll_interval=1, timeout=5),
                report=lambda _line: None,
                resume_request_id=uuid.UUID(request_id),
            )

    assert asyncio.run(rest_resume()) == EXIT_GATE_REFUSED
    assert len(executor.executions) == 1


def test_rejection_by_phone_never_executes(world):
    client, tenant = world["client"], world["tenant"]
    created = client.post(
        f"{API}/requests",
        json={
            "policy_id": world["policy"]["id"],
            "action": ACTION,
            "summary": SUMMARY.format(release=RELEASE),
            "context": {},
        },
        headers=tenant.agent,
    ).json()
    start_calls(world, created["id"])

    async def run(session):
        lines: list[str] = []
        time = FakeTime(
            [
                world["human"](created["id"], world["alice"], "approve"),
                world["human"](created["id"], world["bob"], "reject"),
            ]
        )
        executor = SimulatedDeployment(world["records"])
        code = await run_agent(
            McpGateway(session),
            policy_id=uuid.UUID(world["policy"]["id"]),
            release=RELEASE,
            executor=executor,
            config=WaitConfig(poll_interval=1, timeout=60),
            report=lines.append,
            resume_request_id=uuid.UUID(created["id"]),
            clock=Clock(monotonic=time.monotonic, sleep=time.sleep),
        )
        return code, executor

    code, executor = asyncio.run(with_mcp(world, run))
    assert code == EXIT_REJECTED and executor.executions == []
    assert not world["records"].exists() or not any(world["records"].iterdir())
    assert get_request(client, tenant, created["id"])["status"] == "rejected"


def test_unresolved_condition_never_executes(world):
    client, tenant = world["client"], world["tenant"]

    async def run():
        async with world["api_client"]() as api:
            gateway = RestGateway(api)
            created = await gateway.create(
                uuid.UUID(world["policy"]["id"]),
                ACTION,
                SUMMARY.format(release=RELEASE),
                {},
                "e2e-condition-key",
            )
            await asyncio.to_thread(start_calls, world, str(created.request_id))
            time = FakeTime(
                [
                    world["human"](str(created.request_id), world["alice"], "approve"),
                    world["human"](
                        str(created.request_id),
                        world["bob"],
                        "conditional_approve",
                        "only on a weekday",
                    ),
                ]
            )
            executor = SimulatedDeployment(world["records"])
            code = await run_agent(
                gateway,
                policy_id=uuid.UUID(world["policy"]["id"]),
                release=RELEASE,
                executor=executor,
                config=WaitConfig(poll_interval=1, max_interval=2, timeout=30),
                report=lambda _line: None,
                resume_request_id=created.request_id,
                clock=Clock(monotonic=time.monotonic, sleep=time.sleep),
            )
            return code, executor, created.request_id

    code, executor, request_id = asyncio.run(run())
    assert code == EXIT_TIMEOUT and executor.executions == []
    detail = get_request(client, tenant, str(request_id))
    assert detail["status"] == "pending" and detail["evaluation"]["reason"] == "condition_open"


def test_mcp_cannot_exceed_its_key_or_cross_organizations(world, make_tenant):
    other = make_tenant("Globex")
    approver = create_approver(world["client"], other, "Zed")["id"]
    foreign_policy = make_policy(world["client"], other, [approver])
    foreign = (
        world["client"]
        .post(
            f"{API}/requests",
            json={"policy_id": foreign_policy["id"], "action": "a", "summary": "s"},
            headers=other.agent,
        )
        .json()
    )

    async def probe(session):
        status = await session.call_tool("get_approval_status", {"request_id": foreign["id"]})
        create = await session.call_tool(
            "request_approval", {"policy_id": foreign_policy["id"], "action": "a", "summary": "s"}
        )
        listing = await session.call_tool("list_approval_requests", {})
        return status, create, listing

    status, create, listing = asyncio.run(with_mcp(world, probe))
    assert status.is_error and "not_found" in status.content[0].text
    assert create.is_error and "not_found" in create.content[0].text
    assert listing.structured_content["total"] == 0  # this org has no requests yet

    # An agent key without audit:read cannot read timelines through MCP.
    tenant = world["tenant"]

    async def narrow(_):
        async with (
            ApprovalLineClient(
                ClientSettings("http://approval-line.test", tenant.keys["agent"], 10),
                transport=httpx.ASGITransport(app=world["client"].app),
            ) as api,
            Client(build_server(api)) as session,
        ):
            return await session.call_tool("get_approval_timeline", {"request_id": foreign["id"]})

    denied = asyncio.run(narrow(None))
    assert denied.is_error and "permission_denied" in denied.content[0].text


def test_dashboard_session_sees_the_same_request(world, session_factory):
    client, tenant = world["client"], world["tenant"]
    created = client.post(
        f"{API}/requests",
        json={"policy_id": world["policy"]["id"], "action": ACTION, "summary": "s"},
        headers=tenant.agent,
    ).json()
    create_dashboard_user(session_factory, tenant.organization_id)
    csrf = dashboard_login(client)
    # Schedule calls from the dashboard session (CSRF-protected, real endpoint).
    response = client.post(
        f"{API}/requests/{created['id']}/voice-calls", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 201 and response.json()["created"] == 2
    again = client.post(
        f"{API}/requests/{created['id']}/voice-calls", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert again.status_code == 200 and again.json()["created"] == 0  # no duplicate calls
    world["run_worker"]()
    calls = client.get(f"{API}/requests/{created['id']}/voice-calls").json()["calls"]
    assert {c["provider"] for c in calls} == {"mock"}  # labelled simulated in the UI
    stats = client.get(f"{API}/requests/stats").json()
    assert stats["by_status"]["pending"] == 1
