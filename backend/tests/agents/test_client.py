"""The agent-side REST client never turns failures or malformed data into authorization."""

import asyncio
import uuid

import httpx
import pytest

from approval_line_agents.client import (
    ApiResponseError,
    ApprovalLineClient,
    BackendUnavailableError,
    ClientSettings,
    MalformedResponseError,
    parse_status,
)
from tests.agents.payloads import request_payload

SECRET = "al_super_secret_agent_key_value"


def client_for(handler) -> ApprovalLineClient:
    return ApprovalLineClient(
        ClientSettings("http://backend.test", SECRET, 5), transport=httpx.MockTransport(handler)
    )


def run(coro):
    return asyncio.run(coro)


async def _status(handler, request_id):
    async with client_for(handler) as client:
        return await client.get_status(request_id)


@pytest.mark.parametrize("kind", ["pending", "condition", "reapproval", "rejected", "expired"])
def test_non_approved_states_are_preserved_and_unauthorized(kind):
    rid = uuid.uuid4()
    status = run(_status(lambda r: httpx.Response(200, json=request_payload(kind, rid)), rid))
    assert status.authorized is False
    assert status.status == request_payload(kind, rid)["status"]
    assert status.evaluation.reason == request_payload(kind, rid)["evaluation"]["reason"]


def test_approved_state_is_authorized():
    rid = uuid.uuid4()
    status = run(_status(lambda r: httpx.Response(200, json=request_payload("approved", rid)), rid))
    assert status.authorized is True and status.status == "approved"


def test_the_api_key_is_sent_as_a_header_only():
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("X-API-Key")
        seen["url"] = str(request.url)
        return httpx.Response(200, json=request_payload("pending", rid))

    rid = uuid.uuid4()
    run(_status(handler, rid))
    assert seen["key"] == SECRET and SECRET not in seen["url"]
    assert SECRET not in repr(ClientSettings("http://x", SECRET))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["evaluation"].pop("authorized"),
        lambda p: p["evaluation"].__setitem__("authorized", "true"),
        lambda p: p["evaluation"].__setitem__("authorized", 1),
        lambda p: p["evaluation"].__setitem__("authorized", None),
        lambda p: p.pop("evaluation"),
        lambda p: p.__setitem__("status", "maybe"),
        lambda p: p.pop("status"),
        lambda p: p.__setitem__("id", "not-a-uuid"),
    ],
)
def test_malformed_payloads_are_refused(mutate):
    payload = request_payload("approved", uuid.uuid4())
    mutate(payload)
    with pytest.raises(MalformedResponseError):
        parse_status(payload)


@pytest.mark.parametrize(
    ("kind", "overrides"),
    [
        ("pending", {"authorized": True}),  # authorized while pending
        ("rejected", {"authorized": True}),
        ("expired", {"authorized": True}),
        ("approved", {"authorized": False}),  # approved status without authorization
        ("approved", {"state": "awaiting_approvals"}),
        ("approved", {"next_action": "await_decisions"}),
    ],
)
def test_contradictory_payloads_are_refused(kind, overrides):
    payload = request_payload(kind, uuid.uuid4(), **overrides)
    with pytest.raises(MalformedResponseError) as excinfo:
        parse_status(payload)
    assert excinfo.value.code == "contradictory_response"


def test_a_different_request_in_the_response_is_refused():
    asked = uuid.uuid4()
    other = request_payload("approved", uuid.uuid4())
    with pytest.raises(MalformedResponseError, match="different request"):
        run(_status(lambda r: httpx.Response(200, json=other), asked))


@pytest.mark.parametrize(
    ("response", "error", "code"),
    [
        (
            httpx.Response(401, json={"error": {"code": "api_key_invalid", "message": "x"}}),
            ApiResponseError,
            "api_key_invalid",
        ),
        (
            httpx.Response(403, json={"error": {"code": "insufficient_scope", "message": "x"}}),
            ApiResponseError,
            "insufficient_scope",
        ),
        (
            httpx.Response(404, json={"error": {"code": "request_not_found", "message": "x"}}),
            ApiResponseError,
            "request_not_found",
        ),
        (httpx.Response(500, text="boom"), BackendUnavailableError, "backend_error"),
        (httpx.Response(503, json={}), BackendUnavailableError, "backend_error"),
        (
            httpx.Response(200, text="<html>not json</html>"),
            MalformedResponseError,
            "malformed_response",
        ),
        (httpx.Response(204), MalformedResponseError, "malformed_response"),
    ],
)
def test_http_failures_are_typed_errors(response, error, code):
    with pytest.raises(error) as excinfo:
        run(_status(lambda r: response, uuid.uuid4()))
    assert excinfo.value.code == code
    assert SECRET not in str(excinfo.value)


def test_timeouts_and_network_errors_are_unavailable():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(BackendUnavailableError) as first:
        run(_status(timeout, uuid.uuid4()))
    assert first.value.code == "backend_timeout" and first.value.retryable
    with pytest.raises(BackendUnavailableError) as second:
        run(_status(refused, uuid.uuid4()))
    assert second.value.code == "backend_unreachable"


def test_settings_from_env_validate(monkeypatch):
    monkeypatch.delenv("APPROVAL_LINE_API_URL", raising=False)
    monkeypatch.setenv("APPROVAL_LINE_API_KEY", SECRET)
    with pytest.raises(ValueError, match="API_URL"):
        ClientSettings.from_env()
    monkeypatch.setenv("APPROVAL_LINE_API_URL", "ftp://x")
    with pytest.raises(ValueError, match="http"):
        ClientSettings.from_env()
    monkeypatch.setenv("APPROVAL_LINE_API_URL", "http://127.0.0.1:8000/")
    monkeypatch.delenv("APPROVAL_LINE_API_KEY")
    with pytest.raises(ValueError, match="API_KEY") as excinfo:
        ClientSettings.from_env()
    assert SECRET not in str(excinfo.value)


# --- Idempotent creation (Chunk 5) ----------------------------------------------------------


async def _create(handler, **kwargs):
    async with client_for(handler) as client:
        return await client.create_request(uuid.uuid4(), "deploy", "s", {}, **kwargs)


def test_ambiguous_create_failures_are_retried_with_the_same_key():
    rid = uuid.uuid4()
    seen: list[str | None] = []

    def handler(request):
        seen.append(request.headers.get("Idempotency-Key"))
        if len(seen) == 1:
            raise httpx.ReadTimeout("lost response", request=request)
        if len(seen) == 2:
            return httpx.Response(503, json={})
        return httpx.Response(200, json=request_payload("pending", rid))  # server replay

    status = run(_create(handler, idempotency_key="key-12345678"))
    assert status.request_id == rid and status.authorized is False
    assert seen == ["key-12345678"] * 3


def test_without_a_key_creation_is_never_retried():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("lost response", request=request)

    with pytest.raises(BackendUnavailableError):
        run(_create(handler))
    assert len(calls) == 1 and "Idempotency-Key" not in calls[0].headers


def test_refusals_are_not_retried_even_with_a_key():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            422, json={"error": {"code": "idempotency_key_reused", "message": "x"}}
        )

    with pytest.raises(ApiResponseError) as excinfo:
        run(_create(handler, idempotency_key="key-12345678"))
    assert excinfo.value.code == "idempotency_key_reused" and len(calls) == 1


def test_retries_are_bounded():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(BackendUnavailableError):
        run(_create(handler, idempotency_key="key-12345678", retries=2))
    assert len(calls) == 3
