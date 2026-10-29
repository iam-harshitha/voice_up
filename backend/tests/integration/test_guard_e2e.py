"""End to end: Claude Code guard -> real API -> auto-dial -> mock phone call -> decision.

The guard runs in-process against the real FastAPI app (httpx.ASGITransport), with the
mock voice provider and the real worker and webhook. Nothing leaves the process.
"""

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy import func, select

from app.db.models import ApprovalRequest, VoiceCall
from approval_line_agents.guard import ENV_RULES, ENV_TIMEOUT, EXIT_ALLOW, EXIT_BLOCK, run_guard
from tests.conftest import WEBHOOK_TOKEN
from tests.helpers import API, create_approver, deliver_webhook, list_calls, make_policy

pytestmark = pytest.mark.integration

COMMAND = "./deploy.sh production"


class HumanTime:
    """Guard time: each wait runs the next scripted step (worker, approver), then advances."""

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
    # The guard's key: no voice scope at all.
    tenant = make_tenant(
        voice=["voice:calls:read"],
        guard=["requests:create", "requests:read", "policies:read"],
    )
    approver = create_approver(voice_client, tenant, "Alice", phone_number="+14155550181")
    auto = make_policy(voice_client, tenant, [approver["id"]], name="Auto", auto_dial=True)
    manual = make_policy(voice_client, tenant, [approver["id"]], name="Manual")

    def rules_for(policy) -> str:
        path = tmp_path / f"rules-{policy['name']}.json"
        rule = {
            "name": "production deploy",
            "pattern": r"deploy\.sh\s+production\b",
            "policy_id": policy["id"],
            "action": "deploy_to_production",
            "summary": "Claude Code wants to run `{command}`.",
        }
        path.write_text(json.dumps({"rules": [rule]}), encoding="utf-8")
        return str(path)

    def env(policy) -> dict[str, str]:
        return {
            "APPROVAL_LINE_API_URL": "http://approval-line.test",
            "APPROVAL_LINE_API_KEY": tenant.keys["guard"],
            ENV_RULES: rules_for(policy),
            ENV_TIMEOUT: "120",
        }

    def latest_request_id() -> str:
        page = voice_client.get(f"{API}/requests?limit=1", headers=tenant.agent).json()
        return page["items"][0]["id"]

    def answer(decision: str) -> Callable[[], None]:
        def step() -> None:
            (call,) = list_calls(voice_client, tenant, latest_request_id())
            payload = mock_provider.complete(call["provider_call_id"], decision=decision)
            deliver_webhook(voice_client, payload, WEBHOOK_TOKEN).raise_for_status()
            run_worker()

        return step

    def guard(policy, steps, command=COMMAND, tool_use_id="toolu_e2e_1"):
        raw = json.dumps(
            {
                "session_id": "s-1",
                "cwd": "D:/demo-target",
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "tool_use_id": tool_use_id,
            }
        ).encode()
        return asyncio.run(
            run_guard(
                raw,
                env(policy),
                transport=httpx.ASGITransport(app=voice_client.app),
                clock=HumanTime(steps),
            )
        )

    return {
        "tenant": tenant,
        "auto": auto,
        "manual": manual,
        "guard": guard,
        "answer": answer,
        "run_worker": run_worker,
        "latest": latest_request_id,
        "client": voice_client,
    }


def count(session, model) -> int:
    session.expire_all()
    return session.scalar(select(func.count()).select_from(model))


def test_spoken_approval_lets_the_command_run(world, session):
    # Step 1: the worker dials the auto-scheduled call. Step 2: Alice approves by phone.
    result = world["guard"](world["auto"], [world["run_worker"], world["answer"]("approve")])
    assert (result.exit_code, result.stderr) == (EXIT_ALLOW, "")
    request = (
        world["client"]
        .get(f"{API}/requests/{world['latest']()}", headers=world["tenant"].agent)
        .json()
    )
    assert request["status"] == "approved" and request["evaluation"]["authorized"] is True
    assert request["context"]["command"] == COMMAND
    assert count(session, VoiceCall) == 1  # auto-dial, not the guard, placed the call


def test_spoken_rejection_blocks_the_command(world, session):
    result = world["guard"](world["auto"], [world["run_worker"], world["answer"]("reject")])
    assert result.exit_code == EXIT_BLOCK
    assert "Rejected by an approver" in result.message
    assert world["latest"]() in result.message


def test_an_uncertain_answer_never_runs_the_command(world):
    result = world["guard"](world["auto"], [world["run_worker"], world["answer"]("uncertain")])
    assert result.exit_code == EXIT_BLOCK
    assert "No decision before the timeout" in result.message


def test_rerunning_the_same_tool_call_does_not_dial_again(world, session):
    first = world["guard"](world["auto"], [world["run_worker"], world["answer"]("approve")])
    again = world["guard"](world["auto"], [world["run_worker"]])
    assert first.exit_code == again.exit_code == EXIT_ALLOW
    assert count(session, ApprovalRequest) == 1
    assert count(session, VoiceCall) == 1


def test_policy_without_auto_dial_is_refused_before_any_request(world, session):
    result = world["guard"](world["manual"], [])
    assert result.exit_code == EXIT_BLOCK
    assert "auto-dial off" in result.message
    assert count(session, ApprovalRequest) == 0


def test_unguarded_commands_create_nothing(world, session):
    result = world["guard"](world["auto"], [], command="git status")
    assert result.exit_code == EXIT_ALLOW
    assert count(session, ApprovalRequest) == 0
