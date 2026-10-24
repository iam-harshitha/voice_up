from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.schemas.common import ApiOutput, StrictInput


class VoiceScheduleCreate(StrictInput):
    # Explicit operator action: place a new call to approvers whose latest call ended
    # without a decision. Never done automatically.
    redial_unanswered: bool = False


class VoiceCallRead(ApiOutput):
    """Safe call metadata. The dialled number and provider payloads are never returned."""

    id: UUID
    approver_id: UUID
    generation: int
    provider: str
    status: str
    provider_status: str | None
    provider_call_id: str | None
    initiation_attempts: int
    decision_status: str
    decision_candidate: str | None
    decision_confidence: float | None
    outcome: str | None
    vote_id: UUID | None
    last_error: str | None
    initiated_at: datetime | None
    answered_at: datetime | None
    ended_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SkippedApprover(BaseModel):
    approver_id: UUID
    reason: str


class VoiceScheduleResult(BaseModel):
    request_id: UUID
    created: int
    calls: list[VoiceCallRead]
    skipped: list[SkippedApprover]


class VoiceCallList(BaseModel):
    request_id: UUID
    calls: list[VoiceCallRead]


class WebhookAck(BaseModel):
    status: str  # accepted | duplicate | ignored
