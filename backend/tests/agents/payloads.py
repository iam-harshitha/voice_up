"""REST-shaped request payloads matching the real API (used with mock HTTP transports)."""

import uuid
from typing import Any

POLICY_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
ACTION = "deploy_to_production"


def evaluation(state: str, reason: str, /, **overrides: Any) -> dict[str, Any]:
    base = {
        "state": state,
        "reason": reason,
        "authorized": state == "approved",
        "next_action": "none"
        if state in ("approved", "rejected", "expired")
        else ("resolve_condition" if reason == "condition_open" else "await_decisions"),
        "approvals": 2 if state == "approved" else 1,
        "rejections": 1 if state == "rejected" else 0,
        "required_approvals": 2,
        "veto_triggered": reason == "veto",
        "outstanding_approver_ids": [],
        "condition_approver_ids": [str(uuid.uuid4())] if reason == "condition_open" else [],
        "reapproval_approver_ids": [str(uuid.uuid4())] if reason == "reapproval_required" else [],
        "reapproval_seat_ids": [],
        "deadline_passed": state == "expired",
        "expires_at": None,
    }
    base.update(overrides)
    return base


STATES = {
    "pending": ("pending", "awaiting_approvals", "awaiting_decisions"),
    "condition": ("pending", "waiting_for_condition", "condition_open"),
    "reapproval": ("pending", "waiting_for_condition", "reapproval_required"),
    "approved": ("approved", "approved", "quorum_met"),
    "rejected": ("rejected", "rejected", "veto"),
    "expired": ("expired", "expired", "deadline_passed"),
}


def request_payload(
    kind: str,
    request_id: uuid.UUID,
    *,
    action: str = ACTION,
    summary: str = "Deploy",
    policy_id: uuid.UUID = POLICY_ID,
    **evaluation_overrides: Any,
) -> dict[str, Any]:
    status, state, reason = STATES[kind]
    return {
        "id": str(request_id),
        "policy_id": str(policy_id),
        "policy_version": 1,
        "action": action,
        "summary": summary,
        "status": status,
        "required_approvals": 2,
        "created_at": "2026-10-24T10:00:00Z",
        "updated_at": "2026-10-24T10:00:00Z",
        "decided_at": "2026-10-24T10:05:00Z" if status != "pending" else None,
        "expires_at": None,
        "context": {},
        "authorized_approver_ids": [],
        "votes": [],
        "evaluation": evaluation(state, reason, **evaluation_overrides),
    }
