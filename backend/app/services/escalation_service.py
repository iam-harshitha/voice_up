"""Time-based escalation to backup approvers.

Semantics (docs/policies.md):
- Every ``escalation_after_seconds`` after request creation, each *outstanding* seat (no
  vote, no blocking condition) is assigned the next unused backup, in the policy's backup
  order, until ``max_escalations`` steps exist or the backups run out.
- A backup **replaces** the seat's primary: either of them may decide the seat and the
  first valid vote wins. The quorum and the veto rules never change; a backup inherits
  its seat's veto power.
- The deadline takes precedence: no escalation runs at or after ``expires_at``, and no
  round is scheduled at or after it.
- Runs inside the caller's transaction, which also holds the job row, so a round and the
  job's rescheduling commit together (no duplicate rounds after a crash).
"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import ApprovalRequest, EscalationStep
from app.domain.approval_rules import RequestStatus
from app.services import audit_service, voice_service
from app.services.approval_service import evaluate, request_rules
from app.services.audit_service import AuditEventType


def run_escalation_round(
    session: Session, settings: Settings, request_id: UUID, now: datetime
) -> tuple[str, datetime | None]:
    """Returns (result, next_run_at). ``next_run_at`` None means escalation is finished."""
    request = session.scalar(
        select(ApprovalRequest)
        .where(ApprovalRequest.id == request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if request is None:  # pragma: no cover - RESTRICT foreign key
        return "missing", None
    if request.status != RequestStatus.PENDING.value:
        return f"request_{request.status}", None
    rules = request_rules(request)
    after = rules.escalation_after_seconds
    if not after or not rules.backups or rules.max_escalations == 0:
        return "not_configured", None
    if request.expires_at is not None and now >= request.expires_at:
        return "deadline_passed", None  # expiry has precedence

    elapsed = (now - request.created_at).total_seconds()
    round_no = int(elapsed // after)
    if round_no < 1:
        return "not_due", request.created_at + timedelta(seconds=after)

    steps = list(
        session.scalars(
            select(EscalationStep)
            .where(EscalationStep.request_id == request.id)
            .order_by(EscalationStep.sequence)
        )
    )
    next_at = request.created_at + timedelta(seconds=after * (round_no + 1))
    if request.expires_at is not None and next_at >= request.expires_at:
        next_at = None
    if any(step.round == round_no for step in steps):
        return "round_already_done", next_at  # idempotent re-run

    used = {step.backup_approver_id for step in steps}
    available = [b for b in rules.backups if b not in used]
    remaining = rules.max_escalations - len(steps)
    outstanding = evaluate(session, request, now).outstanding_seats
    created = 0
    for seat in outstanding:  # snapshot order
        if remaining <= 0 or not available:
            break
        backup = available.pop(0)
        call, skipped = voice_service.create_call(
            session, settings, request, backup, reason="escalation"
        )
        step = EscalationStep(
            organization_id=request.organization_id,
            request_id=request.id,
            seat_approver_id=seat,
            backup_approver_id=backup,
            sequence=len(steps) + created + 1,
            round=round_no,
            voice_call_id=call.id if call is not None and skipped is None else None,
            outcome="call_scheduled" if skipped is None else skipped,
        )
        session.add(step)
        session.flush()
        audit_service.record_event(
            session,
            organization_id=request.organization_id,
            request_id=request.id,
            event_type=AuditEventType.ESCALATION_STEP_CREATED,
            data={
                "escalation_step_id": str(step.id),
                "sequence": step.sequence,
                "round": round_no,
                "seat_approver_id": str(seat),
                "backup_approver_id": str(backup),
                "outcome": step.outcome,
                "voice_call_id": str(step.voice_call_id) if step.voice_call_id else None,
            },
        )
        created += 1
        remaining -= 1

    if remaining <= 0 or not available:
        if created:  # record exhaustion once, in the round that used the last step
            audit_service.record_event(
                session,
                organization_id=request.organization_id,
                request_id=request.id,
                event_type=AuditEventType.ESCALATION_EXHAUSTED,
                data={"steps": len(steps) + created, "max_escalations": rules.max_escalations},
            )
        return f"escalated_{created}_exhausted", None
    return f"escalated_{created}", next_at
