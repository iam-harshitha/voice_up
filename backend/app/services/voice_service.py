"""Scheduling and reading voice approval calls (organization-scoped)."""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BusinessRuleError, ConflictError, NotFoundError, ServiceUnavailableError
from app.db.models import ApprovalRequest, ApprovalVote, Approver, VoiceCall, VoiceJob
from app.db.models.voice import DecisionStatus, JobType
from app.db.session import transaction
from app.domain.approval_rules import RequestStatus
from app.services import audit_service
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


def schedule_calls(
    session: Session,
    settings: Settings,
    organization_id: UUID,
    request_id: UUID,
    *,
    redial_unanswered: bool = False,
) -> ScheduleResult:
    """Create one intended call (and its initiation job) per snapshotted approver.

    Idempotent: approvers that already have a call for the request are not called again.
    With ``redial_unanswered`` an operator may explicitly place a new call (next
    generation) to approvers whose latest call ended without a decision, bounded by
    VOICE_MAX_CALL_GENERATIONS. Nothing is ever re-dialled automatically.
    """
    if settings.voice_provider == "disabled":
        raise ServiceUnavailableError("Voice calls are not enabled.", code="voice_unavailable")

    with transaction(session):
        # Locking the request serialises concurrent scheduling and orders the lock
        # before any vote on this request (votes lock the request first as well).
        request = _lock_request(session, organization_id, request_id)
        if request.status != RequestStatus.PENDING.value:
            raise ConflictError(
                f"The request has already been {request.status}.", code="request_already_decided"
            )

        existing = session.scalars(
            select(VoiceCall)
            .where(VoiceCall.request_id == request.id)
            .order_by(VoiceCall.approver_id, VoiceCall.generation.desc())
        ).all()
        latest: dict[UUID, VoiceCall] = {}
        for call in existing:
            latest.setdefault(call.approver_id, call)

        approvers = {
            a.id: a
            for a in session.scalars(
                select(Approver).where(
                    Approver.organization_id == organization_id,
                    Approver.id.in_(request.authorized_approver_ids),
                )
            )
        }
        already_voted = set(
            session.scalars(
                select(ApprovalVote.approver_id).where(ApprovalVote.request_id == request.id)
            )
        )
        result = ScheduleResult(calls=[], created=0)
        for approver_id in request.authorized_approver_ids:  # snapshot order
            previous = latest.get(approver_id)
            if approver_id in already_voted:
                # Their decision is recorded; calling them again could only conflict.
                result.skipped.append({"approver_id": str(approver_id), "reason": "already_voted"})
                if previous is not None:
                    result.calls.append(previous)
                continue
            if previous is not None:
                redial = (
                    redial_unanswered
                    and CallStatus(previous.status) in TERMINAL_STATUSES
                    and previous.decision_status != DecisionStatus.ACCEPTED
                )
                if not redial:
                    result.calls.append(previous)
                    continue
                if previous.generation >= settings.voice_max_call_generations:
                    result.calls.append(previous)
                    result.skipped.append(
                        {"approver_id": str(approver_id), "reason": "max_generations_reached"}
                    )
                    continue

            approver = approvers.get(approver_id)
            reason = None
            if approver is None or not approver.is_active:
                reason = "approver_inactive"
            elif not approver.phone_number:
                reason = "no_phone_number"
            if reason is not None:
                result.skipped.append({"approver_id": str(approver_id), "reason": reason})
                if previous is not None:
                    result.calls.append(previous)
                continue

            call = VoiceCall(
                organization_id=organization_id,
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
                organization_id=organization_id,
                request_id=request.id,
                event_type=AuditEventType.VOICE_CALL_SCHEDULED,
                data={
                    "voice_call_id": str(call.id),
                    "approver_id": str(approver_id),
                    "generation": call.generation,
                    "provider": call.provider,
                },
            )
            result.calls.append(call)
            result.created += 1

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
