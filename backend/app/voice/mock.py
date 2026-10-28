"""Deterministic stand-in for Bolna, for tests and explicitly enabled local development.

It stores executions in Bolna's documented payload shape and reads them back through
the real Bolna normaliser, so tests exercise the same parsing as production. It is
refused when APP_ENV=production (see Settings).
"""

import json
import os
import tempfile
import threading
import uuid
from collections import defaultdict, deque
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.voice.base import (
    CallRequest,
    ErrorKind,
    InitiatedCall,
    ProviderExecution,
    VoiceProviderError,
)
from app.voice.bolna import normalize_execution, parse_execution_id

MOCK_AGENT_ID = "00000000-0000-4000-8000-00000000a9e7"


class MockBehavior(StrEnum):
    ACCEPT = "accept"
    RETRYABLE_ERROR = "retryable_error"  # e.g. connection refused before sending
    PERMANENT_ERROR = "permanent_error"  # e.g. 401 / invalid configuration
    AMBIGUOUS_TIMEOUT = "ambiguous_timeout"  # request sent, response lost
    ACCEPTED_THEN_TIMEOUT = "accepted_then_timeout"  # call created, client never knew


class _MemoryStore:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._data.get(key)
            return json.loads(json.dumps(value)) if value is not None else None

    def put(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._data[key] = json.loads(json.dumps(value))

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [json.loads(json.dumps(v)) for v in self._data.values()]


class _FileStore:
    """Shares simulated executions between the API, worker and CLI processes."""

    def __init__(self, directory: str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        return json.loads(path.read_text("utf-8")) if path.exists() else None

    def put(self, key: str, value: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        os.replace(tmp, self._path(key))

    def all(self) -> list[dict[str, Any]]:
        return [json.loads(p.read_text("utf-8")) for p in sorted(self._dir.glob("*.json"))]


class MockVoiceProvider:
    name = "mock"

    def __init__(self, settings: Settings, state_dir: str | None = None) -> None:
        self._settings = settings
        self.agent_id = MOCK_AGENT_ID
        directory = state_dir or settings.voice_mock_state_dir
        self._store = _FileStore(directory) if directory else _MemoryStore()
        self._behaviors: dict[str, deque[MockBehavior]] = defaultdict(deque)
        self._fetch_failures: dict[str, deque[ErrorKind]] = defaultdict(deque)
        self.initiated: list[CallRequest] = []
        self._lock = threading.Lock()

    # --- test controls -------------------------------------------------------------

    def script(self, phone_number: str, *behaviors: MockBehavior) -> None:
        """Queue behaviours for the next calls to ``phone_number`` (default: accept)."""
        with self._lock:
            self._behaviors[phone_number].extend(behaviors)

    def fail_next_fetch(self, provider_call_id: str, kind: ErrorKind) -> None:
        with self._lock:
            self._fetch_failures[provider_call_id].append(kind)

    def execution_for(self, internal_call_id: str) -> dict[str, Any] | None:
        for payload in self._store.all():
            recipient = payload.get("context_details", {}).get("recipient_data", {})
            if recipient.get("approval_line_call_id") == internal_call_id:
                return payload
        return None

    def set_status(self, provider_call_id: str, status: str) -> dict[str, Any]:
        payload = self._require(provider_call_id)
        payload["status"] = status
        self._store.put(provider_call_id, payload)
        return payload

    def complete(
        self,
        provider_call_id: str,
        *,
        decision: str | None = "approve",
        identity: str | None = "yes",
        confidence: float | None = 0.95,
        identity_confidence: float | None = 0.95,
        condition: str | None = None,
        voicemail: bool = False,
        extracted_data: Any = None,
        status: str = "completed",
    ) -> dict[str, Any]:
        """Finish a simulated conversation and return the Bolna-shaped payload."""
        payload = self._require(provider_call_id)
        if extracted_data is None:
            category: dict[str, Any] = {}
            if decision is not None:
                category[self._settings.bolna_decision_extraction] = {
                    "objective": decision,
                    "confidence": confidence,
                    "subjective": None,
                    "validation": None,
                }
            if identity is not None:
                category[self._settings.bolna_identity_extraction] = {
                    "objective": identity,
                    "confidence": identity_confidence,
                    "subjective": None,
                    "validation": None,
                }
            if condition is not None:
                category[self._settings.bolna_condition_extraction] = {
                    "subjective": condition,
                    "objective": None,
                    "confidence": confidence,
                    "validation": None,
                }
            extracted_data = {self._settings.bolna_extraction_category: category}
        payload.update(
            status=status,
            answered_by_voice_mail=voicemail,
            extracted_data=extracted_data,
            transcript="(simulated conversation)",
        )
        self._store.put(provider_call_id, payload)
        return payload

    def payload(self, provider_call_id: str) -> dict[str, Any]:
        return self._require(provider_call_id)

    def _require(self, provider_call_id: str) -> dict[str, Any]:
        payload = self._store.get(provider_call_id)
        if payload is None:
            raise KeyError(provider_call_id)
        return payload

    # --- provider interface --------------------------------------------------------

    def _create_execution(self, call: CallRequest) -> str:
        execution_id = str(uuid.uuid4())
        self._store.put(
            execution_id,
            {
                "id": execution_id,
                "agent_id": self.agent_id,
                "status": "queued",
                "answered_by_voice_mail": False,
                "extracted_data": None,
                "telephony_data": {"to_number": call.recipient_phone_number},
                "context_details": {
                    "recipient_phone_number": call.recipient_phone_number,
                    "recipient_data": dict(call.variables),
                },
            },
        )
        return execution_id

    def initiate_call(self, call: CallRequest) -> InitiatedCall:
        with self._lock:
            queue = self._behaviors[call.recipient_phone_number]
            behavior = queue.popleft() if queue else MockBehavior.ACCEPT
            self.initiated.append(call)
        if behavior is MockBehavior.RETRYABLE_ERROR:
            raise VoiceProviderError(ErrorKind.RETRYABLE, "connect_error")
        if behavior is MockBehavior.PERMANENT_ERROR:
            raise VoiceProviderError(ErrorKind.PERMANENT, "auth_failed")
        if behavior is MockBehavior.AMBIGUOUS_TIMEOUT:
            raise VoiceProviderError(ErrorKind.AMBIGUOUS, "response_timeout")
        execution_id = self._create_execution(call)
        if behavior is MockBehavior.ACCEPTED_THEN_TIMEOUT:
            raise VoiceProviderError(ErrorKind.AMBIGUOUS, "response_timeout")
        return InitiatedCall(execution_id, "queued")

    def get_execution(self, provider_call_id: str) -> ProviderExecution:
        execution_id = parse_execution_id(provider_call_id)
        if execution_id is None:
            raise VoiceProviderError(ErrorKind.NOT_FOUND, "invalid_execution_id")
        with self._lock:
            failures = self._fetch_failures[execution_id]
            failure = failures.popleft() if failures else None
        if failure is not None:
            raise VoiceProviderError(failure, "simulated_fetch_failure")
        payload = self._store.get(execution_id)
        if payload is None:
            raise VoiceProviderError(ErrorKind.NOT_FOUND, "execution_not_found")
        return normalize_execution(payload, self._settings)
