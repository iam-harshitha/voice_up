"""MCP server: tool discovery, schemas, validation and safe error mapping (mock HTTP).

The integration tests against the real FastAPI application are in
tests/integration/test_chunk4_e2e.py.
"""

import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from approval_line_agents.client import ApprovalLineClient, ClientSettings
from approval_line_agents.mcp_server import build_server
from tests.agents.payloads import POLICY_ID, request_payload

SECRET = "al_mcp_test_secret_key_never_shown"
TOOLS = {
    "request_approval",
    "get_approval_status",
    "list_approval_requests",
    "get_approval_timeline",
    "get_approval_policy",
}


def server_for(handler):
    client = ApprovalLineClient(
        ClientSettings("http://backend.test", SECRET, 5), transport=httpx.MockTransport(handler)
    )
    return build_server(client)


async def _call(handler, tool, arguments):
    async with Client(server_for(handler)) as client:
        return await client.call_tool(tool, arguments)


def call(handler, tool, arguments):
    return asyncio.run(_call(handler, tool, arguments))


def error_text(result) -> str:
    assert result.is_error, result
    return " ".join(c.text for c in result.content)


def test_tools_are_discoverable_with_strict_schemas():
    async def go():
        async with Client(server_for(lambda r: httpx.Response(500))) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in asyncio.run(go())}
    assert set(tools) == TOOLS
    create = tools["request_approval"].input_schema
    assert set(create["required"]) == {"policy_id", "action", "summary"}
    assert create["properties"]["policy_id"]["format"] == "uuid"
    assert create["properties"]["action"]["maxLength"] == 200
    assert create["properties"]["summary"]["maxLength"] == 2000
    listing = tools["list_approval_requests"].input_schema["properties"]
    assert listing["limit"]["maximum"] == 50
    # Every tool declares a structured output schema; nothing can set a decision.
    for tool in tools.values():
        assert tool.output_schema, tool.name
        assert "authorized" not in tool.input_schema.get("properties", {})
        assert "status" not in tools["request_approval"].input_schema["properties"]
    assert not any(name.startswith(("vote", "resolve", "approve", "set_")) for name in tools)
    assert SECRET not in json.dumps([t.model_dump(mode="json") for t in tools.values()])


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_approval_status", {"request_id": "not-a-uuid"}),
        ("get_approval_status", {}),
        ("request_approval", {"policy_id": str(POLICY_ID), "action": "", "summary": "s"}),
        ("request_approval", {"policy_id": str(POLICY_ID), "action": "x" * 201, "summary": "s"}),
        ("request_approval", {"policy_id": "nope", "action": "a", "summary": "s"}),
        ("list_approval_requests", {"status": "authorized"}),
        ("list_approval_requests", {"limit": 51}),
        ("get_approval_timeline", {"request_id": str(uuid.uuid4()), "limit": 0}),
    ],
)
def test_invalid_input_never_reaches_the_backend(tool, arguments):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    result = call(handler, tool, arguments)
    assert result.is_error
    assert calls == []


@pytest.mark.parametrize("kind", ["pending", "condition", "reapproval", "rejected", "expired"])
def test_non_authorized_states_are_preserved_exactly(kind):
    rid = uuid.uuid4()
    payload = request_payload(kind, rid)
    result = call(
        lambda r: httpx.Response(200, json=payload), "get_approval_status", {"request_id": str(rid)}
    )
    assert not result.is_error
    data = result.structured_content
    assert data["authorized"] is False
    assert data["status"] == payload["status"]
    assert data["reason"] == payload["evaluation"]["reason"]
    assert data["next_action"] == payload["evaluation"]["next_action"]
    assert data["request_id"] == str(rid)
    assert "not" in data["guidance"].lower() or "do not" in data["guidance"].lower()


def test_approved_state_is_reported_as_authorized():
    rid = uuid.uuid4()
    result = call(
        lambda r: httpx.Response(200, json=request_payload("approved", rid)),
        "get_approval_status",
        {"request_id": str(rid)},
    )
    data = result.structured_content
    assert data["authorized"] is True and data["status"] == "approved"
    assert data["guidance"].startswith("Authorized")


def test_request_approval_returns_the_backends_initial_state():
    rid = uuid.uuid4()
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(201, json=request_payload("pending", rid))

    result = call(
        handler,
        "request_approval",
        {
            "policy_id": str(POLICY_ID),
            "action": "deploy_to_production",
            "summary": "Deploy v2.4",
            "context": {"release": "v2.4"},
        },
    )
    assert result.structured_content["status"] == "pending"
    assert result.structured_content["authorized"] is False
    assert sent == {
        "policy_id": str(POLICY_ID),
        "action": "deploy_to_production",
        "summary": "Deploy v2.4",
        "context": {"release": "v2.4"},
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["evaluation"].pop("authorized"),
        lambda p: p["evaluation"].__setitem__("authorized", "yes"),
        lambda p: p.__setitem__("status", "pending"),  # authorized=true but pending
        lambda p: p.pop("evaluation"),
    ],
)
def test_malformed_backend_data_is_an_error_never_an_approval(mutate):
    rid = uuid.uuid4()
    payload = request_payload("approved", rid)
    mutate(payload)
    result = call(
        lambda r: httpx.Response(200, json=payload), "get_approval_status", {"request_id": str(rid)}
    )
    assert "untrusted_response" in error_text(result)
    assert result.structured_content is None


@pytest.mark.parametrize(
    ("response", "category"),
    [
        (
            httpx.Response(401, json={"error": {"code": "api_key_invalid", "message": "bad"}}),
            "authentication_failed",
        ),
        (
            httpx.Response(
                403,
                json={
                    "error": {
                        "code": "insufficient_scope",
                        "message": "This API key lacks the 'audit:read' scope.",
                    }
                },
            ),
            "permission_denied",
        ),
        (
            httpx.Response(404, json={"error": {"code": "request_not_found", "message": "nf"}}),
            "not_found",
        ),
        (httpx.Response(502, text="bad gateway"), "backend_unavailable"),
    ],
)
def test_backend_failures_map_to_safe_tool_errors(response, category, caplog):
    caplog.set_level(logging.DEBUG)
    result = call(lambda r: response, "get_approval_status", {"request_id": str(uuid.uuid4())})
    text = error_text(result)
    assert category in text
    assert SECRET not in text and SECRET not in caplog.text
    assert "Traceback" not in text


def test_backend_timeout_is_unknown_state_not_approval():
    def handler(request):
        raise httpx.ConnectTimeout("timeout", request=request)

    result = call(handler, "get_approval_status", {"request_id": str(uuid.uuid4())})
    text = error_text(result)
    assert "backend_unavailable" in text and "do not act" in text


def test_list_and_timeline_and_policy_tools():
    rid = uuid.uuid4()

    def handler(request):
        path = request.url.path
        if path.endswith("/requests"):
            assert request.url.params["limit"] == "5"
            return httpx.Response(
                200,
                json={
                    "items": [request_payload("pending", rid)],
                    "total": 1,
                    "limit": 5,
                    "offset": 0,
                },
            )
        if path.endswith("/audit-events"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "sequence": 7,
                            "request_id": str(rid),
                            "policy_id": None,
                            "event_type": "request.created",
                            "event_data": {"action": "deploy"},
                            "created_at": "2026-10-24T10:00:00Z",
                        }
                    ],
                    "limit": 100,
                    "next_after": None,
                },
            )
        if "/policies/" in path:
            return httpx.Response(
                200,
                json={
                    "id": str(POLICY_ID),
                    "name": "Prod",
                    "description": None,
                    "approval_mode": "unanimous",
                    "required_approvals": 2,
                    "veto_mode": "any_rejection",
                    "is_active": True,
                    "version": 3,
                    "approvers": [{"id": str(uuid.uuid4()), "name": "Alice", "is_active": True}],
                    "veto_approver_ids": [],
                    "backup_approvers": [],
                    "decision_deadline_seconds": 3600,
                    "escalation_after_seconds": None,
                    "max_escalations": 0,
                    "created_at": "2026-10-24T10:00:00Z",
                    "updated_at": "2026-10-24T10:00:00Z",
                },
            )
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "nf"}})

    listing = call(handler, "list_approval_requests", {"limit": 5}).structured_content
    assert listing["total"] == 1 and listing["items"][0]["request_id"] == str(rid)
    timeline = call(handler, "get_approval_timeline", {"request_id": str(rid)}).structured_content
    assert timeline["items"][0]["event_type"] == "request.created"
    policy = call(handler, "get_approval_policy", {"policy_id": str(POLICY_ID)}).structured_content
    assert policy["required_approvals"] == 2 and policy["approvers"][0]["name"] == "Alice"
    assert "phone" not in json.dumps(policy) and "email" not in json.dumps(policy)


def test_stdio_server_starts_lists_tools_and_keeps_stdout_clean():
    """Spawn the real entry point over stdio. The backend URL points at a closed port, so a
    tool call must come back as backend_unavailable (an error), never as a result."""
    backend_dir = Path(__file__).resolve().parents[2]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "approval_line_agents.mcp_server"],
        env={
            "APPROVAL_LINE_API_URL": "http://127.0.0.1:9",
            "APPROVAL_LINE_API_KEY": SECRET,
            "APPROVAL_LINE_TIMEOUT_SECONDS": "2",
        },
        cwd=str(backend_dir),
    )

    async def go():
        async with Client(params, read_timeout_seconds=30) as client:
            tools = (await client.list_tools()).tools
            result = await client.call_tool(
                "get_approval_status", {"request_id": str(uuid.uuid4())}
            )
            return tools, result

    tools, result = asyncio.run(go())
    assert {t.name for t in tools} == TOOLS
    text = error_text(result)
    assert "backend_unavailable" in text and SECRET not in text


def test_stdio_server_refuses_to_start_without_configuration(tmp_path):
    import subprocess

    completed = subprocess.run(
        [sys.executable, "-m", "approval_line_agents.mcp_server"],
        cwd=Path(__file__).resolve().parents[2],
        env={"SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""), "PATH": ""},
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""  # nothing pollutes the protocol stream
    assert "APPROVAL_LINE_API_URL" in completed.stderr
