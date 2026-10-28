"""Bolna adapter against a mocked HTTP transport (no network), using the documented
request/response shapes (see the module docstring of app/voice/bolna.py)."""

import json

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.voice.base import CallRequest, CallStatus, ErrorKind, VoiceProviderError
from app.voice.bolna import BolnaProvider, normalize_execution

DB = "postgresql+psycopg://u:p@127.0.0.1:1/approval_line_test"
API_KEY = "bn-test-key-not-real"
AGENT = "123e4567-e89b-12d3-a456-426655440000"
EXECUTION = "b7140255-af33-4608-8e97-04dd944b8e48"
TOKEN = "t" * 40


def settings(**overrides) -> Settings:
    values = {
        "database_url": DB,
        "voice_provider": "bolna",
        "bolna_api_key": API_KEY,
        "bolna_agent_id": AGENT,
        "voice_webhook_token": TOKEN,
        **overrides,
    }
    # _env_file=None: the developer's local .env must not change what these tests see.
    return Settings(_env_file=None, **values)


def provider_with(handler, **overrides) -> tuple[BolnaProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return BolnaProvider(settings(**overrides), transport=httpx.MockTransport(record)), seen


CALL = CallRequest("call-1", "+14155550101", {"approval_line_call_id": "call-1", "x": "y"})


def test_initiate_call_request_and_response():
    provider, seen = provider_with(
        lambda r: httpx.Response(
            200, json={"message": "done", "status": "queued", "execution_id": EXECUTION}
        )
    )
    result = provider.initiate_call(CALL)
    assert (result.provider_call_id, result.provider_status) == (EXECUTION, "queued")
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.bolna.ai/call"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert json.loads(request.content) == {
        "agent_id": AGENT,
        "recipient_phone_number": "+14155550101",
        "user_data": {"approval_line_call_id": "call-1", "x": "y"},
    }


def test_from_number_is_sent_only_when_configured():
    provider, seen = provider_with(
        lambda r: httpx.Response(200, json={"status": "queued", "execution_id": EXECUTION}),
        bolna_from_phone_number="+14155550100",
    )
    provider.initiate_call(CALL)
    assert json.loads(seen[0].content)["from_phone_number"] == "+14155550100"


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (400, {"error": 400, "message": "agent_id is required"}, ErrorKind.PERMANENT),
        (401, {"message": "Access denied"}, ErrorKind.PERMANENT),
        (403, {}, ErrorKind.PERMANENT),
        (404, {}, ErrorKind.PERMANENT),
        (429, {}, ErrorKind.RETRYABLE),
        (503, {}, ErrorKind.RETRYABLE),
        (500, {}, ErrorKind.AMBIGUOUS),
        (502, {}, ErrorKind.AMBIGUOUS),
        (504, {}, ErrorKind.AMBIGUOUS),
        (200, {"status": "queued"}, ErrorKind.AMBIGUOUS),  # no execution_id
        (200, {"execution_id": "../user/me"}, ErrorKind.AMBIGUOUS),
    ],
)
def test_initiate_error_classification(status, body, kind):
    provider, _ = provider_with(lambda r: httpx.Response(status, json=body))
    with pytest.raises(VoiceProviderError) as caught:
        provider.initiate_call(CALL)
    assert caught.value.kind is kind
    # Errors carry a category only, never provider text or credentials.
    assert API_KEY not in str(caught.value) and "agent_id" not in str(caught.value)


def test_rejection_keeps_the_providers_reason_as_a_sanitized_detail():
    message = "Trial accounts can only make calls to verified phone numbers."
    provider, _ = provider_with(lambda r: httpx.Response(400, json={"message": message}))
    with pytest.raises(VoiceProviderError) as caught:
        provider.initiate_call(CALL)
    error = caught.value
    assert (error.kind, error.category, error.detail) == (
        ErrorKind.PERMANENT,
        "rejected_400",
        message,
    )
    assert message not in str(error)  # detail is for logs only


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (
            {"message": f"Number {CALL.recipient_phone_number} is invalid"},
            "Number <redacted> is invalid",
        ),
        ({"message": "Bad number +1 (415) 555-0101"}, "Bad number <redacted>"),
        ({"message": f"token {TOKEN} rejected"}, "token <redacted> rejected"),
        ({"message": f"key bn-{'a1' * 16} rejected"}, "key <redacted> rejected"),
        ({"message": "contact ops@example.com\nnow"}, "contact <redacted> now"),
        ({"error": "agent not found"}, "agent not found"),
        ({"error": 400, "message": None}, None),
        ({"message": ""}, None),
        (["not", "an", "object"], None),
        ({"message": "word " * 100}, ("word " * 32)[:160]),
        ({"message": "x" * 500}, "<redacted>"),
    ],
)
def test_error_detail_is_redacted_and_bounded(body, detail):
    provider, _ = provider_with(lambda r: httpx.Response(422, json=body))
    with pytest.raises(VoiceProviderError) as caught:
        provider.initiate_call(CALL)
    assert caught.value.detail == detail


def test_non_json_error_body_has_no_detail():
    provider, _ = provider_with(lambda r: httpx.Response(400, content=b"<html>oops</html>"))
    with pytest.raises(VoiceProviderError) as caught:
        provider.initiate_call(CALL)
    assert (caught.value.category, caught.value.detail) == ("rejected_400", None)


def test_rate_limit_stays_retryable_with_its_reason():
    body = {"message": "Number is still activating", "retry_after_seconds": 120}
    provider, _ = provider_with(lambda r: httpx.Response(429, json=body))
    with pytest.raises(VoiceProviderError) as caught:
        provider.initiate_call(CALL)
    assert (caught.value.kind, caught.value.detail) == (
        ErrorKind.RETRYABLE,
        "Number is still activating",
    )


def test_non_json_success_is_ambiguous():
    provider, _ = provider_with(lambda r: httpx.Response(200, content=b"<html>"))
    with pytest.raises(VoiceProviderError) as caught:
        provider.initiate_call(CALL)
    assert caught.value.kind is ErrorKind.AMBIGUOUS


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (httpx.ConnectError("refused"), ErrorKind.RETRYABLE),
        (httpx.ConnectTimeout("slow connect"), ErrorKind.RETRYABLE),
        (httpx.ReadTimeout("no response"), ErrorKind.AMBIGUOUS),
        (httpx.RemoteProtocolError("dropped"), ErrorKind.AMBIGUOUS),
    ],
)
def test_transport_failures(exc, kind):
    def fail(request):
        raise exc

    provider, _ = provider_with(fail)
    with pytest.raises(VoiceProviderError) as caught:
        provider.initiate_call(CALL)
    assert caught.value.kind is kind


def bolna_execution(**overrides):
    payload = {
        "id": EXECUTION,
        "agent_id": AGENT,
        "status": "completed",
        "answered_by_voice_mail": False,
        "transcript": "assistant: ...",
        "telephony_data": {"to_number": "+14155550101", "recording_url": "https://x"},
        "context_details": {
            "recipient_phone_number": "+14155550101",
            "recipient_data": {"approval_line_call_id": "call-1"},
        },
        "extracted_data": {
            "Approval": {
                "Decision": {
                    "subjective": None,
                    "objective": "approve",
                    "confidence": 0.93,
                    "confidence_label": "High",
                    "validation": None,
                },
                "Identity Confirmed": {"objective": "yes", "confidence": 0.97},
            }
        },
    }
    payload.update(overrides)
    return payload


def test_get_execution_fetches_and_normalizes():
    provider, seen = provider_with(lambda r: httpx.Response(200, json=bolna_execution()))
    execution = provider.get_execution(EXECUTION)
    assert str(seen[0].url) == f"https://api.bolna.ai/executions/{EXECUTION}"
    assert seen[0].headers["authorization"] == f"Bearer {API_KEY}"
    assert execution.status is CallStatus.COMPLETED
    assert execution.decision.value == "approve" and execution.decision.confidence == 0.93
    assert execution.identity.value == "yes"
    assert execution.correlation_id == "call-1"
    assert execution.to_number == "+14155550101"


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (404, ErrorKind.NOT_FOUND),
        (401, ErrorKind.PERMANENT),
        (429, ErrorKind.RETRYABLE),
        (500, ErrorKind.RETRYABLE),
    ],
)
def test_get_execution_errors(status, kind):
    provider, _ = provider_with(lambda r: httpx.Response(status, json={}))
    with pytest.raises(VoiceProviderError) as caught:
        provider.get_execution(EXECUTION)
    assert caught.value.kind is kind


def test_get_execution_rejects_non_uuid_ids_without_a_request():
    provider, seen = provider_with(lambda r: httpx.Response(200, json=bolna_execution()))
    with pytest.raises(VoiceProviderError) as caught:
        provider.get_execution("../user/me")
    assert caught.value.kind is ErrorKind.NOT_FOUND and seen == []


def test_get_execution_rejects_mismatched_id():
    other = "11111111-2222-3333-4444-555555555555"
    provider, _ = provider_with(lambda r: httpx.Response(200, json=bolna_execution(id=other)))
    with pytest.raises(VoiceProviderError):
        provider.get_execution(EXECUTION)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("queued", CallStatus.INITIATED),
        ("ringing", CallStatus.RINGING),
        ("in-progress", CallStatus.IN_PROGRESS),
        ("call-disconnected", CallStatus.PROCESSING),
        ("completed", CallStatus.COMPLETED),
        ("no-answer", CallStatus.FAILED),
        ("busy", CallStatus.FAILED),
        ("balance-low", CallStatus.FAILED),
    ],
)
def test_status_mapping(status, expected):
    assert normalize_execution(bolna_execution(status=status), settings()).status is expected


@pytest.mark.parametrize(
    "extracted",
    [
        "not an object",
        {"Approval": "flat string"},
        {"Approval": {"Decision": "approve"}},
        {"Approval": {"Decision": {"objective": 1, "confidence": 0.9}}},
        {"Approval": {"Decision": {"objective": "approve", "confidence": "high"}}},
    ],
)
def test_malformed_extractions_are_flagged(extracted):
    execution = normalize_execution(bolna_execution(extracted_data=extracted), settings())
    assert execution.extraction_malformed is True


def test_missing_or_empty_extractions_are_not_decisions():
    for extracted in (None, {}, {"Other": {}}):
        execution = normalize_execution(bolna_execution(extracted_data=extracted), settings())
        assert execution.decision is None and not execution.extraction_malformed


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"status": "completed"},
        {"id": EXECUTION},
        {"id": "x", "status": "completed"},
        {"id": EXECUTION, "status": "teleported"},
    ],
)
def test_unusable_payloads_raise(payload):
    with pytest.raises(VoiceProviderError):
        normalize_execution(payload, settings())


class TestConfiguration:
    def test_bolna_requires_credentials(self):
        for missing in ("bolna_api_key", "bolna_agent_id", "voice_webhook_token"):
            with pytest.raises(ValidationError, match=missing.upper()):
                settings(**{missing: None})

    def test_mock_is_refused_in_production(self):
        with pytest.raises(ValidationError, match="not allowed"):
            Settings(_env_file=None, database_url=DB, app_env="production", voice_provider="mock")

    def test_short_webhook_token_is_refused(self):
        with pytest.raises(ValidationError, match="32 characters"):
            settings(voice_webhook_token="short")

    def test_plain_http_base_url_is_refused(self):
        with pytest.raises(ValidationError, match="https"):
            settings(bolna_api_base_url="http://api.bolna.ai")

    def test_secrets_are_not_rendered(self):
        rendered = repr(settings()) + str(settings().model_dump())
        assert API_KEY not in rendered and TOKEN not in rendered

    def test_voice_is_disabled_by_default(self):
        assert Settings(_env_file=None, database_url=DB).voice_provider == "disabled"
