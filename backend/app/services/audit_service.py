"""Append-only audit trail. The only operation offered is recording a new event.

Events are added to the caller's session and therefore commit or roll back together with
the business change they describe. Event data must never contain credentials or contact
details; reference entities by ID instead.
"""

from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import AuditEvent


class AuditEventType(StrEnum):
    REQUEST_CREATED = "request.created"
    VOTE_RECORDED = "vote.recorded"
    REQUEST_APPROVED = "request.approved"
    REQUEST_REJECTED = "request.rejected"
    VOICE_CALL_SCHEDULED = "voice.call_scheduled"
    VOICE_CALL_INITIATION_ATTEMPTED = "voice.call_initiation_attempted"
    VOICE_CALL_INITIATED = "voice.call_initiated"
    VOICE_CALL_FAILED = "voice.call_failed"
    VOICE_DECISION_ACCEPTED = "voice.decision_accepted"
    VOICE_DECISION_REJECTED = "voice.decision_rejected"
    VOICE_EVENT_IGNORED = "voice.event_ignored"


def record_event(
    session: Session,
    *,
    organization_id: UUID,
    request_id: UUID,
    event_type: AuditEventType,
    data: dict[str, Any],
) -> AuditEvent:
    event = AuditEvent(
        organization_id=organization_id,
        request_id=request_id,
        event_type=event_type.value,
        event_data=data,
    )
    session.add(event)
    return event
