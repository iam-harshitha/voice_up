"""Append-only audit trail: recording new events and reading timelines. There is no
update or delete operation.

Events are added to the caller's session and therefore commit or roll back together with
the business change they describe. Event data must never contain credentials, contact
details or transcripts; reference entities by ID instead.

Every event describes a request (request_id), a policy (policy_id, for policy
administration) or an approver (approver_id, for Telegram channel links).
"""

from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditEvent


class AuditEventType(StrEnum):
    REQUEST_CREATED = "request.created"
    VOTE_RECORDED = "vote.recorded"
    REQUEST_APPROVED = "request.approved"
    REQUEST_REJECTED = "request.rejected"
    REQUEST_EXPIRED = "request.expired"
    CONDITION_CREATED = "condition.created"
    CONDITION_RESOLVED = "condition.resolved"
    CONDITION_SUPERSEDED = "condition.superseded"
    CONDITION_LAPSED = "condition.lapsed"
    REAPPROVAL_REQUESTED = "condition.reapproval_requested"
    ESCALATION_STEP_CREATED = "escalation.step_created"
    ESCALATION_EXHAUSTED = "escalation.exhausted"
    POLICY_CREATED = "policy.created"
    POLICY_UPDATED = "policy.updated"
    VOICE_CALL_SCHEDULED = "voice.call_scheduled"
    VOICE_AUTO_DIAL_SCHEDULED = "voice.auto_dial_scheduled"
    VOICE_AUTO_DIAL_FAILED = "voice.auto_dial_failed"
    VOICE_CALL_INITIATION_ATTEMPTED = "voice.call_initiation_attempted"
    VOICE_CALL_INITIATED = "voice.call_initiated"
    VOICE_CALL_FAILED = "voice.call_failed"
    VOICE_DECISION_ACCEPTED = "voice.decision_accepted"
    VOICE_DECISION_REJECTED = "voice.decision_rejected"
    VOICE_EVENT_IGNORED = "voice.event_ignored"
    TEXT_PROMPT_SENT = "text.prompt_sent"
    TEXT_PROMPT_FAILED = "text.prompt_failed"
    TEXT_DECISION_ACCEPTED = "text.decision_accepted"
    TEXT_DECISION_REJECTED = "text.decision_rejected"
    CHANNEL_LINK_CREATED = "channel.link_created"
    CHANNEL_LINK_REVOKED = "channel.link_revoked"
    CHANNEL_LINK_REJECTED = "channel.link_rejected"


def record_event(
    session: Session,
    *,
    organization_id: UUID,
    event_type: AuditEventType,
    data: dict[str, Any],
    request_id: UUID | None = None,
    policy_id: UUID | None = None,
    approver_id: UUID | None = None,
) -> AuditEvent:
    if request_id is None and policy_id is None and approver_id is None:
        raise ValueError("an audit event needs a request_id, a policy_id or an approver_id")
    event = AuditEvent(
        organization_id=organization_id,
        request_id=request_id,
        policy_id=policy_id,
        approver_id=approver_id,
        event_type=event_type.value,
        event_data=data,
    )
    session.add(event)
    return event


def recent(session: Session, organization_id: UUID, *, limit: int = 20) -> list[AuditEvent]:
    """The organization's newest events, newest first (by ``sequence``)."""
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.organization_id == organization_id)
            .order_by(AuditEvent.sequence.desc())
            .limit(limit)
        )
    )


def timeline(
    session: Session,
    organization_id: UUID,
    *,
    request_id: UUID | None = None,
    policy_id: UUID | None = None,
    after: int = 0,
    limit: int = 100,
) -> tuple[list[AuditEvent], int | None]:
    """Read-only, organization-scoped timeline in insertion order (``sequence``).

    ``sequence`` is assigned at insert time, so a page read while transactions are still
    in flight can miss an event that commits later with a lower number. Timelines of
    requests that are no longer changing are complete.

    Keyset pagination: pass the returned cursor as ``after`` for the next page.
    """
    query = select(AuditEvent).where(
        AuditEvent.organization_id == organization_id, AuditEvent.sequence > after
    )
    if request_id is not None:
        query = query.where(AuditEvent.request_id == request_id)
    if policy_id is not None:
        query = query.where(AuditEvent.policy_id == policy_id)
    rows = list(session.scalars(query.order_by(AuditEvent.sequence).limit(limit + 1)))
    next_after = rows[limit - 1].sequence if len(rows) > limit else None
    return rows[:limit], next_after
