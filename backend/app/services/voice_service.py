"""Scheduling and reading voice approval calls (organization-scoped)."""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BusinessRuleError, NotFoundError, ServiceUnavailableError
from app.db.base import utcnow
from app.db.models import ApprovalRequest, ApprovalVote, Approver, VoiceCall, VoiceJob
from app.db.models.voice import DecisionStatus, JobType
from app.db.session import transaction
from app.services import audit_service
from app.services.approval_service import require_open
from app.services.audit_service import AuditEventType
from app.voice.base import TERMINAL_STATUSES, CallStatus


@dataclass
class ScheduleResult:
    calls: list[VoiceCall]
    created: int
    skipped: list[dict[str, str]] = field(default_factory=list)


def _lock_request(session: Session, organization_id: UUID, request_id: UUID) -> ApprovalRequest:
    request = session.scalar(
        select(ApprovalRequest)
        .where(ApprovalRequest.id == request_id, ApprovalRequest.organization_id == organization_id)
        .with_for_update()
    )
    if request is None:
        raise NotFoundError("Approval request not found.", code="request_not_found")
    return request


def create_call(
    session: Session,
    settings: Settings,
    request: ApprovalRequest,
    approver_id: UUID,
    *,
    reason: str,
    allow_redial: bool = False,
) -> tuple[VoiceCall | None, str | None]:
    """Create one intended call and its initiation job inside the caller's transaction.

    The caller must hold the request row lock. This is the only place calls are created
    (scheduling, escalation, re-approval), so the duplicate-call and generation limits
    apply to every path. Returns (call, None) for a new call, or (existing_or_None, reason)
    when no new call is created.
    """
    if settings.voice_provider == "disabled":
        return None, "voice_unavailable"
    previous = session.scalar(
        select(VoiceCall)
        .where(VoiceCall.request_id == request.id, VoiceCall.approver_id == approver_id)
        .order_by(VoiceCall.generation.desc())
        .limit(1)
    )
    if previous is not None:
        if CallStatus(previous.status) not in TERMINAL_STATUSES:
            return previous, "call_in_progress"
        if not allow_redial:
            return previous, "already_called"
        if previous.generation >= settings.voice_max_call_generations:
            return previous, "max_generations_reached"
    approver = session.scalar(
        select(Approver).where(
            Approver.id == approver_id, Approver.organization_id == request.organization_id
        )
    )
    if approver is None or not approver.is_active:
        return previous, "approver_inactive"
    if not approver.phone_number:
        return previous, "no_phone_number"

    call = VoiceCall(
        organization_id=request.organization_id,
        request_id=request.id,
        approver_id=approver_id,
        provider=settings.voice_provider,
        generation=(previous.generation + 1) if previous else 1,
        status=CallStatus.QUEUED.value,
    )
    session.add(call)
    session.flush()
    session.add(
        VoiceJob(
            job_type=JobType.INITIATE_CALL,
            voice_call_id=call.id,
            max_attempts=settings.voice_job_max_attempts,
        )
    )
    audit_service.record_event(
        session,
        organization_id=request.organization_id,
        request_id=request.id,
        event_type=AuditEventType.VOICE_CALL_SCHEDULED,
        data={
            "voice_call_id": str(call.id),
            "approver_id": str(approver_id),
            "generation": call.generation,
            "provider": call.provider,
            "reason": reason,
        },
    )
    return call, None


def schedule_reapproval(
    session: Session, settings: Settings, request: ApprovalRequest, approver_id: UUID
) -> None:
    """After a condition is satisfied, ask the same approver again (inside the caller's
    transaction). Without voice the step is still audited; the approver can answer
    through the trusted vote endpoint."""
    call, skipped = create_call(
        session, settings, request, approver_id, reason="reapproval", allow_redial=True
    )
    audit_service.record_event(
        session,
        organization_id=request.organization_id,
        request_id=request.id,
        event_type=AuditEventType.REAPPROVAL_REQUESTED,
        data={
            "approver_id": str(approver_id),
            "voice_call_id": str(call.id) if call is not None and skipped is None else None,
            "call_outcome": "call_scheduled" if skipped is None else skipped,
        },
    )


def schedule_calls(
    session: Session,
    settings: Settings,
    organization_id: UUID,
    request_id: UUID,
    *,
    redial_unanswered: bool = False,
) -> ScheduleResult:
    """Create one intended call (and its initiation job) per snapshotted primary approver.

    Idempotent: approvers that already have a call for the request are not called again.
    With ``redial_unanswered`` an operator may explicitly place a new call (next
    generation) to approvers whose latest call ended without a decision, bounded by
    VOICE_MAX_CALL_GENERATIONS. Nothing is ever re-dialled automatically. Backups are
    only called through escalation.
    """
    if settings.voice_provider == "disabled":
        raise ServiceUnavailableError("Voice calls are not enabled.", code="voice_unavailable")

    with transaction(session):
        # Locking the request serialises concurrent scheduling and orders the lock
        # before any vote on this request (votes lock the request first as well).
        request = _lock_request(session, organization_id, request_id)
        require_open(request, utcnow())

        votes = session.scalars(
            select(ApprovalVote).where(ApprovalVote.request_id == request.id)
        ).all()
        already_voted = {v.approver_id for v in votes}
        decided_seats = {v.seat_approver_id for v in votes}
        result = ScheduleResult(calls=[], created=0)
        for approver_id in request.authorized_approver_ids:  # snapshot order
            previous = session.scalar(
                select(VoiceCall)
                .where(VoiceCall.request_id == request.id, VoiceCall.approver_id == approver_id)
                .order_by(VoiceCall.generation.desc())
                .limit(1)
            )
            if approver_id in already_voted or approver_id in decided_seats:
                # The seat's decision is recorded; calling again could only conflict.
                reason = "already_voted" if approver_id in already_voted else "seat_decided"
                result.skipped.append({"approver_id": str(approver_id), "reason": reason})
                if previous is not None:
                    result.calls.append(previous)
                continue
            redial = bool(
                redial_unanswered
                and previous is not None
                and previous.decision_status != DecisionStatus.ACCEPTED
            )
            call, skipped = create_call(
                session, settings, request, approver_id, reason="scheduled", allow_redial=redial
            )
            if call is not None:
                result.calls.append(call)
            if skipped is None:
                result.created += 1
            elif skipped not in ("already_called", "call_in_progress"):
                result.skipped.append({"approver_id": str(approver_id), "reason": skipped})

        if not result.calls:
            raise BusinessRuleError(
                "None of the request's approvers can be called.", code="no_callable_approvers"
            )
    return result


def list_calls(session: Session, organization_id: UUID, request_id: UUID) -> list[VoiceCall]:
    """Calls ordered by generation, then by the approver's position in the snapshot.

    (created_at alone is not a stable order: calls scheduled together share it.)
    """
    request = session.scalar(
        select(ApprovalRequest).where(
            ApprovalRequest.id == request_id, ApprovalRequest.organization_id == organization_id
        )
    )
    if request is None:
        raise NotFoundError("Approval request not found.", code="request_not_found")
    position = {approver_id: i for i, approver_id in enumerate(request.authorized_approver_ids)}
    calls = session.scalars(
        select(VoiceCall).where(
            VoiceCall.request_id == request_id, VoiceCall.organization_id == organization_id
        )
    ).all()
    return sorted(calls, key=lambda c: (c.generation, position.get(c.approver_id, 99), c.id))
