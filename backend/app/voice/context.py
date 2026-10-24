"""Build the per-call variables the voice agent uses to explain a request.

Only what an approver needs to decide is included. Values are flattened to short
strings; arbitrary JSON is never passed through, and keys that look like secrets or
personal data are dropped.
"""

import re
from typing import Any

from app.db.models import ApprovalRequest, Approver
from app.voice.bolna import CORRELATION_VARIABLE

MAX_SUMMARY_CHARS = 600
MAX_DETAIL_ITEMS = 8
MAX_DETAIL_VALUE_CHARS = 120
_RISK_KEYS = ("risk", "risk_level", "impact", "environment")
_SENSITIVE_KEY = re.compile(
    r"(secret|token|password|passwd|credential|api[_-]?key|auth|cookie|session|private|"
    r"ssn|card|iban|account[_-]?number|phone|email|address)",
    re.IGNORECASE,
)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


def _clean(value: str, limit: int) -> str:
    text = _CONTROL_CHARS.sub(" ", value).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _humanize(key: str) -> str:
    return _clean(key.replace("_", " ").replace("-", " "), 40)


def _scalar(value: Any) -> str | None:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int | float | str):
        text = _clean(str(value), MAX_DETAIL_VALUE_CHARS)
        return text or None
    return None  # nested objects and lists are not read out


def select_details(context: dict[str, Any]) -> tuple[str, str]:
    """Return (details, risk) as short, speakable strings."""
    risk_parts: list[str] = []
    details: list[str] = []
    for key, value in context.items():
        if not isinstance(key, str) or _SENSITIVE_KEY.search(key):
            continue
        text = _scalar(value)
        if text is None:
            continue
        if key.lower() in _RISK_KEYS:
            risk_parts.append(f"{_humanize(key)}: {text}")
        elif len(details) < MAX_DETAIL_ITEMS:
            details.append(f"{_humanize(key)}: {text}")
    return "; ".join(details) or "none provided", "; ".join(risk_parts) or "not specified"


def build_call_variables(
    request: ApprovalRequest, approver: Approver, internal_call_id: str
) -> dict[str, str]:
    details, risk = select_details(request.context or {})
    return {
        CORRELATION_VARIABLE: internal_call_id,
        "approver_name": _clean(approver.name, 100),
        "request_reference": str(request.id)[:8].upper(),
        "requested_action": _clean(request.action, 200),
        "request_summary": _clean(request.summary, MAX_SUMMARY_CHARS),
        "request_details": details,
        "risk_details": risk,
        "required_approvals": str(request.required_approvals),
        "allowed_decisions": "approve or reject",
    }
