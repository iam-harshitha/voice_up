"""Build what an approver is told about a request: the call's variables and the facts
the Telegram message shows.

Only what an approver needs to decide is included. Values are flattened to short
strings; arbitrary JSON is never passed through, and keys that look like secrets or
personal data are dropped.

Each fact is said once: the action is a short phrase ("deploy to production"), the
summary says what will run and where (from the context's command when there is one,
since the guard's summary repeats the action), and the risk stands alone. The details
skip the command and directory already in the summary; they stay available to the voice
agent, but the recommended prompt does not read them.
"""

import re
from dataclasses import dataclass
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


# Context keys the summary already states (when a command is present).
_IN_SUMMARY = ("command", "working_directory")


def select_details(context: dict[str, Any], *, skip: tuple[str, ...] = ()) -> tuple[str, str]:
    """Return (details, risk) as short, speakable strings."""
    risk_parts: list[str] = []
    details: list[str] = []
    for key, value in context.items():
        if not isinstance(key, str) or _SENSITIVE_KEY.search(key) or key in skip:
            continue
        text = _scalar(value)
        if text is None:
            continue
        if key.lower() in _RISK_KEYS:
            # "Risk: high", not "Risk: risk: high": the label already says it.
            bare = key.lower() in ("risk", "risk_level")
            risk_parts.append(text if bare else f"{_humanize(key)}: {text}")
        elif len(details) < MAX_DETAIL_ITEMS:
            details.append(f"{_humanize(key)}: {text}")
    return "; ".join(details) or "none provided", "; ".join(risk_parts) or "not specified"


def action_phrase(action: str) -> str:
    """'deploy_to_production' -> 'deploy to production' (speakable; no separators)."""
    return _clean(re.sub(r"[._-]+", " ", action), 200)


def _context_text(context: dict[str, Any], key: str) -> str | None:
    # The same filters as select_details: never a sensitive-looking key, scalars only.
    if _SENSITIVE_KEY.search(key):
        return None
    return _scalar(context.get(key)) if isinstance(context.get(key), str) else None


@dataclass(frozen=True)
class RequestFacts:
    action: str  # "deploy to production"
    command: str | None  # "./deploy.sh production" (from the context), if any
    folder: str | None  # last part of the working directory, if any
    summary: str  # the request's summary (cleaned), used when there is no command
    risk: str  # "risk: high" or "not specified"
    details: str  # remaining context, without what the other facts already say
    reference: str  # "3F2A9C1E"


def request_facts(request: ApprovalRequest) -> RequestFacts:
    context = request.context or {}
    command = _context_text(context, "command")
    directory = _context_text(context, "working_directory")
    folder = re.split(r"[\\/]", directory.rstrip("\\/"))[-1] if directory else None
    details, risk = select_details(context, skip=_IN_SUMMARY if command else ())
    return RequestFacts(
        action=action_phrase(request.action),
        command=command,
        folder=folder or None,
        summary=_clean(request.summary, MAX_SUMMARY_CHARS),
        risk=risk,
        details=details,
        reference=str(request.id)[:8].upper(),
    )


def spoken_summary(facts: RequestFacts) -> str:
    """What will happen, without repeating the action: 'run X in folder' or the summary."""
    if facts.command is None:
        return facts.summary
    return f"run {facts.command}" + (f" in {facts.folder}" if facts.folder else "")


def build_call_variables(
    request: ApprovalRequest, approver: Approver, internal_call_id: str
) -> dict[str, str]:
    facts = request_facts(request)
    return {
        CORRELATION_VARIABLE: internal_call_id,
        "approver_name": _clean(approver.name, 100),
        "request_reference": facts.reference,
        "requested_action": facts.action,
        "request_summary": spoken_summary(facts),
        "request_details": facts.details,
        "risk_details": facts.risk,
        "required_approvals": str(request.required_approvals),
        "allowed_decisions": "approve or reject",
    }
