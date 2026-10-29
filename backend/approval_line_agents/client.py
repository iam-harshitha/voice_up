"""Strict async client for the Approval Line REST API (agent side).

Design rules:
- Authenticates with one API key (X-API-Key). The key is never logged, never included
  in exception messages and never returned to callers.
- Every response is validated. A response that does not match the expected shape, or
  that contradicts itself (for example ``authorized: true`` on a request whose status is
  not ``approved``), raises ``MalformedResponseError``. Nothing is ever defaulted to
  "authorized".
- HTTP and network failures become typed errors with a stable ``code``; they are never
  turned into a successful result.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, ValidationError

RequestStatus = Literal["pending", "approved", "rejected", "expired"]
TERMINAL_STATUSES: frozenset[str] = frozenset({"approved", "rejected", "expired"})

DEFAULT_TIMEOUT_SECONDS = 10.0
# Same rule as the API's Idempotency-Key header.
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:\-]{8,128}$")
MAX_ERROR_MESSAGE = 300


class ApprovalLineError(Exception):
    """A failure talking to Approval Line. ``code`` is stable and machine-readable."""

    def __init__(self, code: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message[:MAX_ERROR_MESSAGE]
        self.status_code = status_code

    @property
    def retryable(self) -> bool:
        return False


class BackendUnavailableError(ApprovalLineError):
    """Network failure, timeout or 5xx: the outcome is unknown, not negative or positive."""

    @property
    def retryable(self) -> bool:
        return True


class MalformedResponseError(ApprovalLineError):
    """The backend answered with something this client cannot trust."""


class ApiResponseError(ApprovalLineError):
    """The backend refused the call (4xx) with its standard error body."""


# --- Response models (validated; unknown extra fields are ignored) ---------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Evaluation(_Model):
    state: str
    reason: str
    authorized: StrictBool
    next_action: str
    approvals: StrictInt
    rejections: StrictInt
    required_approvals: StrictInt
    veto_triggered: StrictBool
    outstanding_approver_ids: list[UUID]
    condition_approver_ids: list[UUID]
    reapproval_approver_ids: list[UUID] = []
    deadline_passed: StrictBool
    expires_at: datetime | None


class ApprovalStatus(_Model):
    """The authoritative state of one approval request, as reported by the backend."""

    request_id: UUID
    policy_id: UUID
    policy_version: StrictInt
    action: str
    summary: str
    status: RequestStatus
    created_at: datetime
    decided_at: datetime | None
    expires_at: datetime | None
    evaluation: Evaluation

    @property
    def authorized(self) -> bool:
        return self.evaluation.authorized

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class RequestSummary(_Model):
    request_id: UUID
    policy_id: UUID
    action: str
    summary: str
    status: RequestStatus
    created_at: datetime
    decided_at: datetime | None
    expires_at: datetime | None


class RequestPage(_Model):
    items: list[RequestSummary]
    total: StrictInt
    limit: StrictInt
    offset: StrictInt


class TimelineEvent(_Model):
    sequence: StrictInt
    event_type: str
    created_at: datetime
    event_data: dict[str, Any]


class Timeline(_Model):
    request_id: UUID
    items: list[TimelineEvent]
    next_after: StrictInt | None


class PolicyApprover(_Model):
    id: UUID
    name: str
    is_active: StrictBool


class Policy(_Model):
    id: UUID
    name: str
    description: str | None
    approval_mode: str
    required_approvals: StrictInt
    veto_mode: str
    version: StrictInt
    is_active: StrictBool
    approvers: list[PolicyApprover]
    veto_approver_ids: list[UUID]
    backup_approvers: list[PolicyApprover]
    decision_deadline_seconds: StrictInt | None
    escalation_after_seconds: StrictInt | None
    max_escalations: StrictInt
    # None when the backend predates the field (unknown, not "off").
    auto_dial: StrictBool | None = None


def parse_status(payload: Any) -> ApprovalStatus:
    """Validate a request-detail payload (REST shape) into an ``ApprovalStatus``.

    Raises ``MalformedResponseError`` if fields are missing or have the wrong type, or if
    the authorization signals contradict each other.
    """
    if not isinstance(payload, dict):
        raise MalformedResponseError("malformed_response", "Request payload is not an object.")
    data = dict(payload)
    if "request_id" not in data and "id" in data:
        data["request_id"] = data["id"]
    try:
        status = ApprovalStatus.model_validate(data)
    except ValidationError as exc:
        raise MalformedResponseError(
            "malformed_response",
            f"Request payload failed validation ({exc.error_count()} problem(s)).",
        ) from None
    check_consistency(status)
    return status


def check_consistency(status: ApprovalStatus) -> None:
    """Refuse self-contradictory authorization data instead of guessing."""
    evaluation = status.evaluation
    approved_status = status.status == "approved"
    if evaluation.authorized != approved_status:
        raise MalformedResponseError(
            "contradictory_response",
            "The response's 'authorized' flag contradicts the request status.",
        )
    if evaluation.authorized and (
        evaluation.state != "approved" or evaluation.next_action != "none"
    ):
        raise MalformedResponseError(
            "contradictory_response",
            "The response claims authorization but its evaluation is not final approval.",
        )
    if status.status != "pending" and evaluation.state != status.status:
        raise MalformedResponseError(
            "contradictory_response", "The evaluation state contradicts the request status."
        )


@dataclass(frozen=True)
class ClientSettings:
    base_url: str
    api_key: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, prefix: str = "APPROVAL_LINE_") -> ClientSettings:
        return cls.from_env_mapping(os.environ, prefix)

    @classmethod
    def from_env_mapping(
        cls, env: Mapping[str, str], prefix: str = "APPROVAL_LINE_"
    ) -> ClientSettings:
        base_url = env.get(f"{prefix}API_URL", "").strip()
        api_key = env.get(f"{prefix}API_KEY", "").strip()
        if not base_url:
            raise ValueError(f"{prefix}API_URL is not set")
        if not api_key:
            raise ValueError(f"{prefix}API_KEY is not set")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"{prefix}API_URL must start with http:// or https://")
        raw_timeout = env.get(f"{prefix}TIMEOUT_SECONDS", "").strip()
        timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
        if not 0 < timeout <= 120:
            raise ValueError(f"{prefix}TIMEOUT_SECONDS must be in (0, 120]")
        return cls(base_url.rstrip("/"), api_key, timeout)

    def __repr__(self) -> str:  # never show the key
        return f"ClientSettings(base_url={self.base_url!r}, timeout={self.timeout_seconds})"


class ApprovalLineClient:
    """One reusable HTTP connection pool; use as an async context manager."""

    def __init__(
        self,
        settings: ClientSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        api_prefix: str = "/api/v1",
    ) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.base_url + api_prefix,
            headers={"X-API-Key": settings.api_key, "Accept": "application/json"},
            timeout=httpx.Timeout(settings.timeout_seconds),
            transport=transport,
            follow_redirects=False,
        )

    async def __aenter__(self) -> ApprovalLineClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise BackendUnavailableError(
                "backend_timeout", "Approval Line did not respond in time."
            ) from None
        except httpx.HTTPError:
            raise BackendUnavailableError(
                "backend_unreachable", "Approval Line could not be reached."
            ) from None
        if response.status_code >= 500:
            raise BackendUnavailableError(
                "backend_error",
                f"Approval Line returned HTTP {response.status_code}.",
                status_code=response.status_code,
            )
        try:
            body = response.json()
        except ValueError:
            raise MalformedResponseError(
                "malformed_response",
                f"Approval Line returned a non-JSON body (HTTP {response.status_code}).",
                status_code=response.status_code,
            ) from None
        if response.status_code >= 400:
            error = body.get("error") if isinstance(body, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            raise ApiResponseError(
                code if isinstance(code, str) else f"http_{response.status_code}",
                message if isinstance(message, str) else f"HTTP {response.status_code}",
                status_code=response.status_code,
            )
        if response.status_code not in (200, 201):
            raise MalformedResponseError(
                "malformed_response",
                f"Unexpected HTTP {response.status_code} from Approval Line.",
                status_code=response.status_code,
            )
        return body

    async def create_request(
        self,
        policy_id: UUID,
        action: str,
        summary: str,
        context: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        retries: int = 2,
    ) -> ApprovalStatus:
        """Create one request.

        With ``idempotency_key``, an ambiguous failure (timeout, network error, 5xx) is
        retried up to ``retries`` times with the same key: the server replays the original
        request instead of creating a duplicate. Without a key nothing is retried, because
        a retry could create a second request.
        """
        payload = {
            "policy_id": str(policy_id),
            "action": action,
            "summary": summary,
            "context": context or {},
        }
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        attempts = 1 + (max(0, retries) if idempotency_key else 0)
        for attempt in range(attempts):
            try:
                body = await self._call("POST", "/requests", json=payload, headers=headers)
                return parse_status(body)
            except BackendUnavailableError:
                if attempt + 1 >= attempts:
                    raise
                await asyncio.sleep(min(2.0, 0.25 * 2**attempt))
        raise AssertionError("unreachable")  # pragma: no cover

    async def get_status(self, request_id: UUID) -> ApprovalStatus:
        status = parse_status(await self._call("GET", f"/requests/{request_id}"))
        if status.request_id != request_id:
            raise MalformedResponseError(
                "request_mismatch", "The backend returned a different request than asked for."
            )
        return status

    async def list_requests(
        self, *, status: RequestStatus | None = None, limit: int = 20, offset: int = 0
    ) -> RequestPage:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status is not None:
            params["status"] = status
        body = await self._call("GET", "/requests", params=params)
        try:
            items = [{**item, "request_id": item.get("id")} for item in body["items"]]
            return RequestPage.model_validate({**body, "items": items})
        except (ValidationError, KeyError, TypeError, AttributeError):
            raise MalformedResponseError(
                "malformed_response", "Request list failed validation."
            ) from None

    async def get_timeline(self, request_id: UUID, *, after: int = 0, limit: int = 100) -> Timeline:
        body = await self._call(
            "GET", f"/requests/{request_id}/audit-events", params={"after": after, "limit": limit}
        )
        try:
            return Timeline.model_validate({**body, "request_id": request_id})
        except (ValidationError, TypeError):
            raise MalformedResponseError(
                "malformed_response", "Timeline failed validation."
            ) from None

    async def get_policy(self, policy_id: UUID) -> Policy:
        body = await self._call("GET", f"/policies/{policy_id}")
        try:
            return Policy.model_validate(body)
        except ValidationError:
            raise MalformedResponseError(
                "malformed_response", "Policy failed validation."
            ) from None
