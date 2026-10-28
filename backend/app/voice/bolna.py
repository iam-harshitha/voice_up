"""Bolna adapter. Contract verified against the official docs on 2026-10-24:

- POST {base}/call  (Authorization: Bearer <key>)
  body: agent_id, recipient_phone_number, optional from_phone_number, user_data
  200:  {"message": "...", "status": "queued", "execution_id": "<uuid>"}
  https://www.bolna.ai/docs/api-reference/calls/make
- GET {base}/executions/{execution_id} returns the execution object, the same shape
  Bolna POSTs to the agent's webhook on every status change.
  https://www.bolna.ai/docs/api-reference/executions/get_execution
  https://www.bolna.ai/docs/api-reference/executions/payload
- Status enum: https://www.bolna.ai/docs/list-phone-call-status
- Extractions: extracted_data[<category>][<extraction>] = {objective, confidence, ...}
  https://www.bolna.ai/docs/prompting/extraction-output
- 429 when rate limited: https://www.bolna.ai/docs/api-reference/limits

Bolna documents no idempotency key for POST /call, so any failure after the request may
have reached Bolna is reported as AMBIGUOUS and never retried automatically.
"""

import re
from typing import Any
from uuid import UUID

import httpx

from app.core.config import Settings
from app.voice.base import (
    CallRequest,
    CallStatus,
    ErrorKind,
    ExtractedAnswer,
    InitiatedCall,
    ProviderExecution,
    VoiceProviderError,
)

CORRELATION_VARIABLE = "approval_line_call_id"

_STATUS_MAP = {
    "scheduled": CallStatus.INITIATED,
    "queued": CallStatus.INITIATED,
    "rescheduled": CallStatus.INITIATED,
    "initiated": CallStatus.INITIATED,
    "ringing": CallStatus.RINGING,
    "in-progress": CallStatus.IN_PROGRESS,
    # The line dropped; post-call processing (incl. extraction) is still running.
    "call-disconnected": CallStatus.PROCESSING,
    "completed": CallStatus.COMPLETED,
    "busy": CallStatus.FAILED,
    "no-answer": CallStatus.FAILED,
    "canceled": CallStatus.FAILED,
    "failed": CallStatus.FAILED,
    "stopped": CallStatus.FAILED,
    "error": CallStatus.FAILED,
    "balance-low": CallStatus.FAILED,
}


def parse_execution_id(value: object) -> str | None:
    """Bolna execution IDs are UUIDs; anything else is rejected before use in a URL."""
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def map_status(provider_status: str) -> CallStatus | None:
    return _STATUS_MAP.get(provider_status)


MAX_ERROR_DETAIL = 160
_DIGIT_RUN = re.compile(r"\+?\d[\d\s().-]{5,}\d")  # phone numbers, account numbers
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-]{24,}")  # keys, tokens, IDs
_EMAIL = re.compile(r"\S+@\S+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def error_detail(response: httpx.Response) -> str | None:
    """Bolna's explanation of a refused request, safe to log.

    Only the documented ``message`` (or a string ``error``) is read, never the whole
    body. Phone numbers, e-mail addresses and anything token-like are redacted.
    """
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    message = data.get("message")
    if not isinstance(message, str):
        message = data.get("error") if isinstance(data.get("error"), str) else None
    if not message:
        return None
    text = _CONTROL.sub(" ", message)
    text = _EMAIL.sub("<redacted>", text)
    text = _DIGIT_RUN.sub("<redacted>", text)
    text = _LONG_TOKEN.sub("<redacted>", text)
    text = " ".join(text.split())[:MAX_ERROR_DETAIL]
    return text or None


def _answer(block: Any) -> tuple[ExtractedAnswer | None, bool]:
    """Read one extraction result. Returns (answer, malformed)."""
    if block is None:
        return None, False
    if not isinstance(block, dict):
        return None, True
    value = block.get("objective")
    confidence = block.get("confidence")
    if value is not None and not isinstance(value, str):
        return None, True
    if confidence is not None and (
        isinstance(confidence, bool) or not isinstance(confidence, int | float)
    ):
        return None, True
    return ExtractedAnswer(value, float(confidence) if confidence is not None else None), False


MAX_CONDITION_TEXT = 500


def _free_text(block: Any) -> tuple[str | None, bool]:
    """Read a free-text extraction (its ``subjective`` answer). Returns (text, malformed)."""
    if block is None:
        return None, False
    if not isinstance(block, dict):
        return None, True
    text = block.get("subjective")
    if text is None:
        return None, False
    if not isinstance(text, str):
        return None, True
    text = " ".join(text.split())[:MAX_CONDITION_TEXT]
    return text or None, False


def normalize_execution(payload: Any, settings: Settings) -> ProviderExecution:
    """Convert a Bolna execution object into the internal representation."""
    if not isinstance(payload, dict):
        raise VoiceProviderError(ErrorKind.PERMANENT, "malformed_execution")
    execution_id = parse_execution_id(payload.get("id"))
    provider_status = payload.get("status")
    if execution_id is None:
        raise VoiceProviderError(ErrorKind.PERMANENT, "malformed_execution")
    if not isinstance(provider_status, str):
        raise VoiceProviderError(ErrorKind.PERMANENT, "malformed_execution")
    status = map_status(provider_status)
    if status is None:
        raise VoiceProviderError(ErrorKind.PERMANENT, "unsupported_status")

    telephony = (
        payload.get("telephony_data") if isinstance(payload.get("telephony_data"), dict) else {}
    )
    context = (
        payload.get("context_details") if isinstance(payload.get("context_details"), dict) else {}
    )
    recipient_data = (
        context.get("recipient_data") if isinstance(context.get("recipient_data"), dict) else {}
    )
    to_number = telephony.get("to_number") or context.get("recipient_phone_number")
    correlation = recipient_data.get(CORRELATION_VARIABLE)

    decision = identity = None
    condition_text = None
    malformed = False
    extracted = payload.get("extracted_data")
    if extracted is not None:
        if not isinstance(extracted, dict):
            malformed = True
        else:
            category = extracted.get(settings.bolna_extraction_category)
            if category is not None and not isinstance(category, dict):
                malformed = True
            elif isinstance(category, dict):
                decision, bad_decision = _answer(category.get(settings.bolna_decision_extraction))
                identity, bad_identity = _answer(category.get(settings.bolna_identity_extraction))
                condition_text, bad_condition = _free_text(
                    category.get(settings.bolna_condition_extraction)
                )
                malformed = bad_decision or bad_identity or bad_condition

    return ProviderExecution(
        provider_call_id=execution_id,
        status=status,
        provider_status=provider_status,
        agent_id=payload.get("agent_id") if isinstance(payload.get("agent_id"), str) else None,
        to_number=to_number if isinstance(to_number, str) else None,
        answered_by_voicemail=payload.get("answered_by_voice_mail") is True,
        correlation_id=correlation if isinstance(correlation, str) else None,
        decision=decision,
        identity=identity,
        condition_text=condition_text,
        extraction_malformed=malformed,
    )


class BolnaProvider:
    name = "bolna"

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        if settings.bolna_api_key is None or not settings.bolna_agent_id:
            raise ValueError("Bolna provider requires BOLNA_API_KEY and BOLNA_AGENT_ID")
        self._settings = settings
        self._agent_id = self.agent_id = settings.bolna_agent_id
        timeout = settings.voice_http_timeout_seconds
        self._client = httpx.Client(
            base_url=settings.bolna_api_base_url,
            headers={
                "Authorization": f"Bearer {settings.bolna_api_key.get_secret_value()}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def initiate_call(self, call: CallRequest) -> InitiatedCall:
        body: dict[str, Any] = {
            "agent_id": self._agent_id,
            "recipient_phone_number": call.recipient_phone_number,
            "user_data": call.variables,
        }
        if self._settings.bolna_from_phone_number:
            body["from_phone_number"] = self._settings.bolna_from_phone_number
        try:
            response = self._client.post("/call", json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # The request never reached Bolna.
            raise VoiceProviderError(ErrorKind.RETRYABLE, "connect_error") from exc
        except httpx.TimeoutException as exc:
            # Sent, but no response: Bolna may have queued the call.
            raise VoiceProviderError(ErrorKind.AMBIGUOUS, "response_timeout") from exc
        except httpx.HTTPError as exc:
            raise VoiceProviderError(ErrorKind.AMBIGUOUS, "transport_error") from exc

        code = response.status_code
        if code in (200, 201):
            try:
                data = response.json()
            except ValueError as exc:
                raise VoiceProviderError(ErrorKind.AMBIGUOUS, "malformed_response") from exc
            execution_id = parse_execution_id(
                data.get("execution_id") if isinstance(data, dict) else None
            )
            if execution_id is None:
                raise VoiceProviderError(ErrorKind.AMBIGUOUS, "malformed_response")
            status = data.get("status")
            return InitiatedCall(execution_id, status if isinstance(status, str) else "queued")
        if code in (401, 403):
            raise VoiceProviderError(ErrorKind.PERMANENT, "auth_failed")
        if code == 429:
            raise VoiceProviderError(ErrorKind.RETRYABLE, "rate_limited", error_detail(response))
        if code == 503:
            raise VoiceProviderError(ErrorKind.RETRYABLE, "provider_unavailable")
        if 400 <= code < 500:
            # e.g. 400 "Trial accounts can only make calls to verified phone numbers."
            raise VoiceProviderError(
                ErrorKind.PERMANENT, f"rejected_{code}", error_detail(response)
            )
        # 500/502/504 etc.: the call may or may not have been created.
        raise VoiceProviderError(ErrorKind.AMBIGUOUS, f"provider_error_{code}")

    def get_execution(self, provider_call_id: str) -> ProviderExecution:
        execution_id = parse_execution_id(provider_call_id)
        if execution_id is None:
            raise VoiceProviderError(ErrorKind.NOT_FOUND, "invalid_execution_id")
        try:
            response = self._client.get(f"/executions/{execution_id}")
        except httpx.HTTPError as exc:
            raise VoiceProviderError(ErrorKind.RETRYABLE, "fetch_failed") from exc
        code = response.status_code
        if code == 404:
            raise VoiceProviderError(ErrorKind.NOT_FOUND, "execution_not_found")
        if code in (401, 403):
            raise VoiceProviderError(ErrorKind.PERMANENT, "auth_failed")
        if code == 429 or code >= 500:
            raise VoiceProviderError(ErrorKind.RETRYABLE, f"fetch_status_{code}")
        if code != 200:
            raise VoiceProviderError(ErrorKind.PERMANENT, f"fetch_status_{code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise VoiceProviderError(ErrorKind.PERMANENT, "malformed_execution") from exc
        execution = normalize_execution(payload, self._settings)
        if execution.provider_call_id != execution_id:
            raise VoiceProviderError(ErrorKind.PERMANENT, "execution_id_mismatch")
        return execution
