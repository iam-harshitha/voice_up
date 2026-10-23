import json
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator

from app.domain.approval_rules import RequestStatus
from app.schemas.common import ApiOutput, StrictInput, bounded_text
from app.schemas.vote import VoteRead

MAX_CONTEXT_BYTES = 16 * 1024

Action = bounded_text(200)
Summary = bounded_text(2000)


class ApprovalRequestCreate(StrictInput):
    """What an agent may submit. Status, snapshot, organization and decision are derived
    server-side; supplying any of them is rejected as an unknown field."""

    policy_id: UUID
    action: Action
    summary: Summary
    # Typed as a dict, so scalars and arrays are rejected by validation.
    context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("context")
    @classmethod
    def _bound_context(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, separators=(",", ":")).encode()) > MAX_CONTEXT_BYTES:
            raise ValueError(f"context must serialise to at most {MAX_CONTEXT_BYTES} bytes")
        return value


class ApprovalRequestSummary(ApiOutput):
    id: UUID
    policy_id: UUID
    policy_version: int
    action: str
    summary: str
    status: RequestStatus
    required_approvals: int
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None


class ApprovalRequestRead(ApprovalRequestSummary):
    context: dict[str, Any]
    authorized_approver_ids: list[UUID]
    votes: list[VoteRead]
