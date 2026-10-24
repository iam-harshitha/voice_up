from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from app.schemas.common import ApiOutput, StrictInput, bounded_text
from app.schemas.request import EvaluationRead

ConditionText = bounded_text(500)
Note = bounded_text(1000)
Evidence = bounded_text(500)


class ConditionCreate(StrictInput):
    """Trusted-integration input (same trust level as votes:write)."""

    approver_id: UUID
    description: ConditionText


class ConditionResolve(StrictInput):
    outcome: Literal["satisfied", "not_satisfied"]
    note: Note
    # A reference to evidence held elsewhere (ticket, change ID, URL). Not interpreted.
    evidence_reference: Evidence | None = None


class ConditionRead(ApiOutput):
    id: UUID
    approver_id: UUID
    seat_approver_id: UUID
    source: str
    voice_call_id: UUID | None
    description: str
    status: str
    policy_version: int
    created_at: datetime
    closed_at: datetime | None
    resolved_by_api_key_id: UUID | None
    resolution_note: str | None
    evidence_reference: str | None


class RequestStateRead(BaseModel):
    id: UUID
    status: str
    evaluation: EvaluationRead


class ConditionResult(BaseModel):
    condition: ConditionRead
    request: RequestStateRead
    created: bool


class EscalationStepRead(ApiOutput):
    id: UUID
    sequence: int
    round: int
    seat_approver_id: UUID
    backup_approver_id: UUID
    voice_call_id: UUID | None
    outcome: str
    created_at: datetime


class AuditEventRead(ApiOutput):
    sequence: int
    event_type: str
    event_data: dict[str, Any]
    created_at: datetime


class AuditTimeline(BaseModel):
    items: list[AuditEventRead]
    limit: int
    # Pass as ?after= to fetch the next page; null when there are no more events.
    next_after: int | None
