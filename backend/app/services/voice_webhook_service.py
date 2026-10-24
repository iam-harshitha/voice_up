"""Inbound provider events: durable ingestion and transactional application.

Flow:
  1. ingest_webhook()  - authenticated webhook -> minimal inbox row + process_event job,
                          committed before the endpoint answers 2xx.
  2. worker            - for terminal statuses, re-fetches the execution from the provider
                          API with our credentials (the webhook body is never trusted for
                          decisions), then calls apply_execution().
  3. apply_execution() - one transaction: lifecycle transition, decision interpretation,
                          vote via approval_service.record_vote, audit events, and marking
                          the inbox event processed.
"""

import hashlib
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.db.base import utcnow
from app.db.models import ApprovalRequest, VoiceCall, VoiceJob, VoiceProviderEvent
from app.db.models.voice import DecisionStatus, EventStatus, JobType
from app.db.session import transaction
from app.services import approval_service, audit_service
from app.services.audit_service import AuditEventType
from app.voice.base import TERMINAL_STATUSES, CallStatus, ProviderExecution, can_transition
from app.voice.bolna import map_status, parse_execution_id
from app.voice.decision import ExpectedCall, interpret

logger = logging.getLogger(__name__)

MAX_PROVIDER_FIELD = 64


class InvalidWebhookError(Exception):
    """The authenticated payload is not a usable provider event."""


@dataclass(frozen=True)
class IngestResult:
    outcome: str  # accepted | duplicate | ignored
    event_id: UUID | None = None


def dedup_key(provider_call_id: str, provider_status: str, provider_updated_at: str | None) -> str:
    raw = f"{provider_call_id}|{provider_status}|{provider_updated_at or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()


def ingest_webhook(
    session: Session, settings: Settings, provider: str, payload: Any
) -> IngestResult:
    """Store one authenticated delivery. Only the fields needed later are kept."""
    if not isinstance(payload, dict):
        raise InvalidWebhookError("payload must be a JSON object")
    provider_call_id = parse_execution_id(payload.get("id"))
    provider_status = payload.get("status")
    if provider_call_id is None or not isinstance(provider_status, str):
        raise InvalidWebhookError("missing or invalid id/status")
    if map_status(provider_status) is None:
        logger.info("voice.webhook_ignored", extra={"reason": "unsupported_status"})
        return IngestResult("ignored")
    updated_at = payload.get("updated_at")
    updated_at = updated_at[:MAX_PROVIDER_FIELD] if isinstance(updated_at, str) else None

    with transaction(session):
        event_id = session.scalar(
            insert(VoiceProviderEvent)
            .values(
                provider=provider,
                dedup_key=dedup_key(provider_call_id, provider_status, updated_at),
                provider_call_id=provider_call_id,
                provider_status=provider_status[:32],
                provider_updated_at=updated_at,
                status=EventStatus.RECEIVED,
            )
            .on_conflict_do_nothing(constraint="uq_voice_provider_events_provider_dedup_key")
            .returning(VoiceProviderEvent.id)
        )
        if event_id is None:
            outcome = IngestResult("duplicate")
        else:
            session.add(
                VoiceJob(
                    job_type=JobType.PROCESS_EVENT,
                    event_id=event_id,
                    max_attempts=settings.voice_job_max_attempts,
                )
            )
            outcome = IngestResult("accepted", event_id)
    logger.info(
        "voice.webhook_received",
        extra={
            "provider_call_id": provider_call_id,
            "provider_status": provider_status[:32],
            "dedup": outcome.outcome,
        },
    )
    return outcome


def _audit(session: Session, call: VoiceCall, event_type: AuditEventType, **data: Any) -> None:
    audit_service.record_event(
        session,
        organization_id=call.organization_id,
        request_id=call.request_id,
        event_type=event_type,
        data={"voice_call_id": str(call.id), **data},
    )


def _find_call(
    session: Session,
    provider: str,
    execution: ProviderExecution,
    agent_id: str,
    *,
    trusted: bool,
) -> tuple[VoiceCall | None, bool]:
    """Resolve the persisted call. Returns (call, adopted).

    Normally by (provider, provider_call_id). Only for an execution fetched from the
    provider API with our own credentials (``trusted``) may a call whose initiation
    outcome was lost be adopted via the echoed correlation ID, and only if the agent and
    dialled number match.
    """
    call = session.scalar(
        select(VoiceCall).where(
            VoiceCall.provider == provider,
            VoiceCall.provider_call_id == execution.provider_call_id,
        )
    )
    if call is not None or not trusted or not execution.correlation_id:
        return call, False
    try:
        candidate_id = UUID(execution.correlation_id)
    except ValueError:
        return None, False
    candidate = session.get(VoiceCall, candidate_id)
    if (
        candidate is None
        or candidate.provider != provider
        or candidate.provider_call_id is not None
        or candidate.status not in (CallStatus.INITIATING.value, CallStatus.UNKNOWN.value)
        or execution.agent_id != agent_id
        or candidate.recipient_phone_number is None
        or (
            execution.to_number is not None
            and execution.to_number != candidate.recipient_phone_number
        )
    ):
        return None, False
    return candidate, True


def _record_decision(
    session: Session,
    settings: Settings,
    call: VoiceCall,
    request: ApprovalRequest,
    execution: ProviderExecution,
    agent_id: str,
) -> str:
    interpretation = interpret(
        execution,
        ExpectedCall(
            provider_call_id=call.provider_call_id or "",
            internal_call_id=str(call.id),
            agent_id=agent_id,
            recipient_phone_number=call.recipient_phone_number,
            min_confidence=settings.voice_min_decision_confidence,
        ),
    )
    call.decision_candidate = interpretation.candidate.value if interpretation.candidate else None
    call.decision_confidence = interpretation.confidence

    # A verified conditional approval is a condition, checked before "no vote".
    if interpretation.is_condition:
        return _record_condition(session, call, request, interpretation.condition or "")

    if not interpretation.is_vote:
        reason = interpretation.reason.value if interpretation.reason else "no_decision"
        call.decision_status = DecisionStatus.NO_DECISION
        call.outcome = reason
        _audit(
            session,
            call,
            AuditEventType.VOICE_DECISION_REJECTED,
            reason=reason,
            candidate=call.decision_candidate,
        )
        return reason

    vote = interpretation.vote
    if vote is None:  # pragma: no cover - is_vote guarantees a vote
        raise RuntimeError("interpretation without vote")
    try:
        # Savepoint: if the approval service refuses, the call outcome still commits.
        with session.begin_nested():
            outcome = approval_service.record_vote(
                session,
                call.organization_id,
                request.id,
                call.approver_id,
                vote,
                source="voice",
                source_details={"voice_call_id": str(call.id)},
            )
    except AppError as exc:
        call.decision_status = DecisionStatus.REFUSED
        call.outcome = exc.code[:48]
        _audit(
            session,
            call,
            AuditEventType.VOICE_DECISION_REJECTED,
            reason=exc.code,
            candidate=call.decision_candidate,
        )
        return exc.code
    except IntegrityError:
        call.decision_status = DecisionStatus.REFUSED
        call.outcome = "vote_conflict"
        _audit(
            session,
            call,
            AuditEventType.VOICE_DECISION_REJECTED,
            reason="vote_conflict",
            candidate=call.decision_candidate,
        )
        return "vote_conflict"

    call.decision_status = DecisionStatus.ACCEPTED
    call.vote_id = outcome.vote.id
    call.outcome = "vote_recorded" if outcome.created else "vote_already_recorded"
    _audit(
        session,
        call,
        AuditEventType.VOICE_DECISION_ACCEPTED,
        vote=vote.value,
        vote_id=str(outcome.vote.id),
        request_status=outcome.request.status,
    )
    return call.outcome


def _record_condition(
    session: Session, call: VoiceCall, request: ApprovalRequest, description: str
) -> str:
    """A verified conditional approval becomes a condition record, never a vote."""
    try:
        with session.begin_nested():
            outcome = approval_service.record_condition(
                session,
                call.organization_id,
                request.id,
                call.approver_id,
                description,
                source="voice",
                voice_call_id=call.id,
            )
    except AppError as exc:
        call.decision_status = DecisionStatus.REFUSED
        call.outcome = exc.code[:48]
        _audit(
            session,
            call,
            AuditEventType.VOICE_DECISION_REJECTED,
            reason=exc.code,
            candidate=call.decision_candidate,
        )
        return exc.code
    call.decision_status = DecisionStatus.CONDITIONAL
    call.outcome = "condition_recorded" if outcome.created else "condition_already_recorded"
    _audit(
        session,
        call,
        AuditEventType.VOICE_DECISION_ACCEPTED,
        decision=call.decision_candidate,
        condition_id=str(outcome.condition.id),
        request_status=outcome.request.status,
    )
    return call.outcome


def apply_execution(
    session: Session,
    settings: Settings,
    provider: str,
    agent_id: str,
    execution: ProviderExecution,
    *,
    trusted: bool,
    event_id: UUID | None = None,
) -> str:
    """Apply one provider observation atomically. Returns a short result code.

    ``trusted`` is True only when ``execution`` was fetched from the provider API with
    our credentials. Decisions are evaluated only from trusted executions.
    """
    now = utcnow()
    with transaction(session):
        call, adopted = _find_call(session, provider, execution, agent_id, trusted=trusted)
        if call is None:
            result = "unmatched"
            _finish_event(session, event_id, EventStatus.UNMATCHED, result, None, now)
            return result

        # Lock order: request, then call (the same order votes and scheduling use).
        request = session.scalar(
            select(ApprovalRequest)
            .where(
                ApprovalRequest.id == call.request_id,
                ApprovalRequest.organization_id == call.organization_id,
            )
            .with_for_update()
        )
        call = session.scalar(
            select(VoiceCall)
            .where(VoiceCall.id == call.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if request is None or call is None:  # pragma: no cover - RESTRICT foreign keys
            raise RuntimeError("voice call or request disappeared")
        # Everything below happens at the moment the locks were granted, not when the
        # event arrived (the deadline itself is re-checked by approval_service).
        now = utcnow()

        # Re-check the binding under the lock: the call may have been bound to a different
        # execution (or moved on) since the unlocked lookup.
        bound_elsewhere = (
            call.provider_call_id is not None
            and call.provider_call_id != execution.provider_call_id
        )
        no_longer_adoptable = adopted and (
            call.provider_call_id is not None
            or call.status not in (CallStatus.INITIATING.value, CallStatus.UNKNOWN.value)
        )
        if bound_elsewhere or no_longer_adoptable:
            result = "unmatched"
            _finish_event(session, event_id, EventStatus.UNMATCHED, result, None, now)
            return result

        if adopted and call.provider_call_id is None:
            call.provider_call_id = execution.provider_call_id
            if call.status in (CallStatus.INITIATING.value, CallStatus.UNKNOWN.value):
                # The lost initiation is now confirmed by the provider itself.
                call.status = CallStatus.INITIATED.value
                call.initiated_at = call.initiated_at or now
            _audit(session, call, AuditEventType.VOICE_CALL_INITIATED, recovered=True)

        current = CallStatus(call.status)
        new = execution.status
        if not can_transition(current, new):
            result = "duplicate" if current == new else "stale"
            # Audit only late events after the call has finished; routine reordering of
            # in-flight statuses is just logged.
            if event_id is not None and current in TERMINAL_STATUSES:
                _audit(
                    session,
                    call,
                    AuditEventType.VOICE_EVENT_IGNORED,
                    reason=result,
                    provider_status=execution.provider_status[:32],
                )
            _finish_event(session, event_id, EventStatus.PROCESSED, result, call.id, now)
            return result

        call.status = new.value
        call.provider_status = execution.provider_status[:32]
        call.last_event_at = now
        if new in (CallStatus.IN_PROGRESS, CallStatus.PROCESSING, CallStatus.COMPLETED):
            call.answered_at = call.answered_at or now
        if new in (CallStatus.PROCESSING, CallStatus.COMPLETED, CallStatus.FAILED):
            call.ended_at = call.ended_at or now

        result = f"status_{new.value}"
        if new is CallStatus.COMPLETED:
            if trusted:
                result = _record_decision(session, settings, call, request, execution, agent_id)
            else:  # pragma: no cover - the worker always fetches terminal states
                call.decision_status = DecisionStatus.NO_DECISION
                call.outcome = "unverified_completion"
                result = call.outcome
        elif new is CallStatus.FAILED:
            call.decision_status = DecisionStatus.NO_DECISION
            call.outcome = execution.provider_status[:48]
            _audit(
                session,
                call,
                AuditEventType.VOICE_CALL_FAILED,
                status=new.value,
                reason=call.outcome,
            )
        _finish_event(session, event_id, EventStatus.PROCESSED, result, call.id, now)
    logger.info(
        "voice.event_applied",
        extra={
            "voice_call_id": str(call.id),
            "request_id": str(call.request_id),
            "call_status": call.status,
            "result": result,
        },
    )
    return result


def _finish_event(
    session: Session,
    event_id: UUID | None,
    status: str,
    result: str,
    call_id: UUID | None,
    now,
) -> None:
    if event_id is None:
        return
    event = session.get(VoiceProviderEvent, event_id, with_for_update=True)
    if event is None:  # pragma: no cover - FK guarantees existence
        return
    event.status = status
    event.result = result[:48]
    event.voice_call_id = call_id
    event.processed_at = now


def requeue_unmatched_events(
    session: Session, settings: Settings, provider: str, provider_call_id: str
) -> int:
    """Re-process events that arrived before the call's provider ID was persisted."""
    events = session.scalars(
        select(VoiceProviderEvent).where(
            VoiceProviderEvent.provider == provider,
            VoiceProviderEvent.provider_call_id == provider_call_id,
            VoiceProviderEvent.status == EventStatus.UNMATCHED,
        )
    ).all()
    for event in events:
        event.status = EventStatus.RECEIVED
        job = session.scalar(select(VoiceJob).where(VoiceJob.event_id == event.id))
        if job is not None:
            job.status = "pending"
            job.run_after = utcnow()
            job.lease_expires_at = None
            job.locked_by = None
            job.attempts = 0
        else:  # pragma: no cover - every event is created with its job
            session.add(
                VoiceJob(
                    job_type=JobType.PROCESS_EVENT,
                    event_id=event.id,
                    max_attempts=settings.voice_job_max_attempts,
                )
            )
    return len(events)
