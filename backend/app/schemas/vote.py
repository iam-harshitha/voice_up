from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.domain.approval_rules import RequestStatus, VoteChoice
from app.schemas.common import ApiOutput, StrictInput


class VoteCreate(StrictInput):
    approver_id: UUID
    vote: VoteChoice


class VoteRead(ApiOutput):
    id: UUID
    approver_id: UUID
    seat_approver_id: UUID
    vote: VoteChoice
    created_at: datetime


class VoteRequestState(BaseModel):
    id: UUID
    status: RequestStatus
    decided_at: datetime | None


class VoteResult(BaseModel):
    vote: VoteRead
    request: VoteRequestState
    # False when an identical vote already existed and nothing new was recorded.
    created: bool
