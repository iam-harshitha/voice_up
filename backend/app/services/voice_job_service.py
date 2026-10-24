"""PostgreSQL-backed voice job queue and its handlers.

Queue: jobs are claimed with ``FOR UPDATE SKIP LOCKED`` and leased. A job whose lease
expires (worker crash) becomes claimable again. Completion checks lease ownership.

No handler ever holds a database transaction or row lock while talking to the provider:
  phase 1 (transaction)  decide and record intent ("initiating"), commit
  phase 2 (no locks)     provider HTTP request with bounded timeouts
  phase 3 (transaction)  record the result

The external call itself cannot be rolled back. If phase 1 committed "initiating" and the
worker died before phase 3, the provider may or may not have placed the call; such a
call is marked ``unknown`` and never re-dialled automatically. It can still be recovered
if the provider later reports an execution carrying its correlation ID.
"""

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import ApprovalRequest, Approver, VoiceCall, VoiceJob, VoiceProviderEvent
from app.db.models.voice import DecisionStatus, EventStatus, JobStatus, JobType
from app.db.session import transaction
from app.domain.approval_rules import RequestStatus
from app.services import audit_service
from app.services.audit_service import AuditEventType
from app.services.voice_webhook_service import apply_execution, requeue_unmatched_events
from app.voice import provider_agent_id
from app.voice.base import (
    TERMINAL_STATUSES,
    CallRequest,
    CallStatus,
    ErrorKind,
    InitiatedCall,
    ProviderExecution,
    VoiceProvider,
    VoiceProviderError,
)
from app.voice.bolna import map_status
from app.voice.context import build_call_variables

logger = logging.getLogger(__name__)
_random = secrets.SystemRandom()

ACTIVE_CALL_STATUSES = (
    CallStatus.INITIATED.value,
    CallStatus.RINGING.value,
    CallStatus.IN_PROGRESS.value,
    CallStatus.PROCESSING.value,
)


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    job_type: str
    voice_call_id: UUID | None
    event_id: UUID | None
    request_id: UUID | None
    attempts: int
    max_attempts: int
    # True when this claim took over an expired lease (the previous worker died).
    recovered: bool


def _require[T](value: T | None) -> T:
    if value is None:  # rows referenced by RESTRICT foreign keys cannot disappear
        raise RuntimeError("referenced row is missing")
    return value


def backoff_seconds(settings: Settings, attempts: int) -> float:
    """Exponential backoff with jitter in [50%, 100%] of the capped delay."""
    delay = min(
        settings.voice_retry_max_seconds,
        settings.voice_retry_base_seconds * (2 ** max(0, attempts - 1)),
    )
    return delay * _random.uniform(0.5, 1.0)


# --- Queue primitives ---------------------------------------------------------------


def claim_next(
    session: Session, settings: Settings, worker_id: str, now: datetime | None = None
) -> ClaimedJob | None:
    now = now or utcnow()
    with transaction(session):
        job = session.scalar(
            select(VoiceJob)
            .where(
                or_(
                    and_(VoiceJob.status == JobStatus.PENDING, VoiceJob.run_after <= now),
                    and_(
                        VoiceJob.status == JobStatus.PROCESSING,
                        VoiceJob.lease_expires_at < now,
                    ),
                )
            )
            .order_by(VoiceJob.run_after, VoiceJob.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return None
        recovered = job.status == JobStatus.PROCESSING
        job.status = JobStatus.PROCESSING
        job.lease_expires_at = now + timedelta(seconds=settings.voice_job_lease_seconds)
        job.locked_by = worker_id[:64]
        job.attempts += 1
        claimed = ClaimedJob(
            job.id,
            job.job_type,
            job.voice_call_id,
            job.event_id,
            job.request_id,
            job.attempts,
            job.max_attempts,
            recovered,
        )
    logger.info(
        "voice.job_claimed",
        extra={
            "job_id": str(claimed.id),
            "job_type": claimed.job_type,
            "attempt": claimed.attempts,
            "recovered": recovered,
        },
    )
    return claimed


def _owned_job(session: Session, job: ClaimedJob, worker_id: str) -> VoiceJob | None:
    row = session.get(VoiceJob, job.id, with_for_update=True, populate_existing=True)
    if row is None or row.status != JobStatus.PROCESSING or row.locked_by != worker_id[:64]:
        logger.warning("voice.job_lease_lost", extra={"job_id": str(job.id)})
        return None
    return row


def _succeed(row: VoiceJob | None) -> None:
    if row is not None:
        row.status = JobStatus.SUCCEEDED
        row.lease_expires_at = None
        row.last_error = None


def _fail(row: VoiceJob | None, category: str) -> None:
    if row is not None:
        row.status = JobStatus.FAILED
        row.lease_expires_at = None
        row.last_error = category[:64]


def _retry_or_fail(row: VoiceJob | None, settings: Settings, category: str) -> bool:
    """Reschedule with backoff. Returns False when attempts are exhausted."""
    if row is None:
        return True
    if row.attempts >= row.max_attempts:
        _fail(row, category)
        return False
    row.status = JobStatus.PENDING
    row.lease_expires_at = None
    row.locked_by = None
    row.last_error = category[:64]
    row.run_after = utcnow() + timedelta(seconds=backoff_seconds(settings, row.attempts))
    return True


def _audit(session: Session, call: VoiceCall, event_type: AuditEventType, **data) -> None:
    audit_service.record_event(
        session,
        organization_id=call.organization_id,
        request_id=call.request_id,
        event_type=event_type,
        data={"voice_call_id": str(call.id), **data},
    )


def _end_call(
    session: Session, call: VoiceCall, status: CallStatus, outcome: str, error: str | None = None
) -> None:
    call.status = status.value
    call.outcome = outcome[:48]
    call.last_error = error[:64] if error else None
    call.ended_at = call.ended_at or utcnow()
    if call.decision_status == DecisionStatus.AWAITING:
        call.decision_status = DecisionStatus.NO_DECISION
    _audit(session, call, AuditEventType.VOICE_CALL_FAILED, status=status.value, reason=outcome)


# --- initiate_call ------------------------------------------------------------------


def run_initiate(
    session_factory: sessionmaker[Session],
    provider: VoiceProvider,
    settings: Settings,
    job: ClaimedJob,
    worker_id: str,
) -> str:
    # Phase 1: decide, and durably record the intent before contacting the provider.
    with session_factory() as session, transaction(session):
        row = _owned_job(session, job, worker_id)
        if row is None:
            return "lease_lost"
        call = _require(session.get(VoiceCall, job.voice_call_id, with_for_update=True))
        status = CallStatus(call.status)
        if status in TERMINAL_STATUSES or status not in (CallStatus.QUEUED, CallStatus.INITIATING):
            _succeed(row)
            return "nothing_to_do"
        if status is CallStatus.INITIATING:
            # A previous attempt recorded its intent but never its result: the provider
            # may already be calling this person. Never dial again automatically.
            _end_call(session, call, CallStatus.UNKNOWN, "initiation_interrupted")
            _fail(row, "ambiguous_after_crash")
            return "unknown"

        request = _require(session.get(ApprovalRequest, call.request_id))
        if request.status != RequestStatus.PENDING.value:
            _end_call(session, call, CallStatus.CANCELED, f"request_{request.status}")
            _succeed(row)
            return "canceled"
        approver = session.get(Approver, call.approver_id)
        if approver is None or not approver.is_active or not approver.phone_number:
            reason = "no_phone_number" if approver and approver.is_active else "approver_inactive"
            _end_call(session, call, CallStatus.FAILED, reason)
            _fail(row, reason)
            return reason

        call.status = CallStatus.INITIATING.value
        call.initiation_attempts += 1
        call.recipient_phone_number = approver.phone_number
        call_request = CallRequest(
            internal_call_id=str(call.id),
            recipient_phone_number=approver.phone_number,
            variables=build_call_variables(request, approver, str(call.id)),
        )
        _audit(
            session,
            call,
            AuditEventType.VOICE_CALL_INITIATION_ATTEMPTED,
            attempt=call.initiation_attempts,
            job_id=str(job.id),
        )

    # Phase 2: the provider request. No transaction or lock is held here.
    result: InitiatedCall | None = None
    error: VoiceProviderError | None = None
    try:
        result = provider.initiate_call(call_request)
    except VoiceProviderError as exc:
        error = exc

    # Phase 3: record what happened. The call row is updated even if this worker lost
    # its job lease, because the provider-side effect has already occurred.
    with session_factory() as session, transaction(session):
        row = _owned_job(session, job, worker_id)
        call = _require(session.get(VoiceCall, job.voice_call_id, with_for_update=True))
        log = {"voice_call_id": str(call.id), "job_id": str(job.id), "attempt": job.attempts}
        if result is not None:
            if call.provider_call_id is None:
                call.provider_call_id = result.provider_call_id
                if call.status in (CallStatus.INITIATING.value, CallStatus.UNKNOWN.value):
                    call.status = CallStatus.INITIATED.value
                call.provider_status = result.provider_status[:32]
                call.initiated_at = utcnow()
                call.last_event_at = call.initiated_at
                call.last_error = None
                if call.decision_status == DecisionStatus.NO_DECISION and call.outcome == (
                    "initiation_interrupted"
                ):
                    call.decision_status = DecisionStatus.AWAITING
                    call.outcome = None
                    call.ended_at = None
                _audit(
                    session,
                    call,
                    AuditEventType.VOICE_CALL_INITIATED,
                    provider_call_id=result.provider_call_id,
                )
                requeue_unmatched_events(session, settings, call.provider, result.provider_call_id)
            _succeed(row)
            logger.info("voice.call_initiated", extra=log)
            return "initiated"

        error = _require(error)
        log["error_category"] = error.category
        if call.status != CallStatus.INITIATING.value or call.provider_call_id is not None:
            # While our request was in flight, the call was resolved elsewhere: another
            # worker took over an expired lease (and marked it unknown), or a trusted
            # provider observation adopted it. That newer state wins; never overwrite it.
            if row is not None:
                _fail(row, f"superseded_{error.category}")
            logger.warning("voice.call_initiation_superseded", extra=log)
            return "superseded"
        if error.kind is ErrorKind.RETRYABLE:
            if _retry_or_fail(row, settings, error.category):
                call.status = CallStatus.QUEUED.value
                call.last_error = error.category[:64]
                logger.warning("voice.call_initiation_retry", extra=log)
                return "retry"
            _end_call(session, call, CallStatus.FAILED, "initiation_failed", error.category)
            logger.error("voice.call_initiation_exhausted", extra=log)
            return "failed"
        if error.kind is ErrorKind.PERMANENT:
            _end_call(session, call, CallStatus.FAILED, "initiation_rejected", error.category)
            _fail(row, error.category)
            logger.error("voice.call_initiation_rejected", extra=log)
            return "failed"
        # AMBIGUOUS (or anything unexpected): the call may exist; do not retry.
        _end_call(session, call, CallStatus.UNKNOWN, "initiation_unknown", error.category)
        _fail(row, error.category)
        logger.error("voice.call_initiation_ambiguous", extra=log)
        return "unknown"


# --- process_event ------------------------------------------------------------------


def run_process_event(
    session_factory: sessionmaker[Session],
    provider: VoiceProvider,
    settings: Settings,
    job: ClaimedJob,
    worker_id: str,
) -> str:
    with session_factory() as session:
        event = _require(session.get(VoiceProviderEvent, job.event_id))
        provider_name, provider_call_id = event.provider, event.provider_call_id
        provider_status, event_id = event.provider_status, event.id
        already_done = event.status != EventStatus.RECEIVED
    if already_done:
        # Effects were committed before a crash prevented marking the job done.
        with session_factory() as session, transaction(session):
            _succeed(_owned_job(session, job, worker_id))
        return "already_processed"

    status = map_status(provider_status)
    if status is None:  # pragma: no cover - filtered at ingestion
        with session_factory() as session, transaction(session):
            _finish(session, job, worker_id, event_id, EventStatus.IGNORED, "unsupported_status")
        return "ignored"

    trusted = False
    if status in TERMINAL_STATUSES:
        # Only spend a provider API call when the answer can matter: a known call that
        # is still open, or an open call awaiting adoption. Otherwise a token-bearing
        # flood of forged events would exhaust our (per-account) Bolna rate limit.
        with session_factory() as session:
            known = session.scalar(
                select(VoiceCall).where(
                    VoiceCall.provider == provider_name,
                    VoiceCall.provider_call_id == provider_call_id,
                )
            )
            adoptable = known is None and session.scalar(
                select(VoiceCall.id)
                .where(
                    VoiceCall.provider == provider_name,
                    VoiceCall.provider_call_id.is_(None),
                    VoiceCall.status.in_((CallStatus.INITIATING.value, CallStatus.UNKNOWN.value)),
                )
                .limit(1)
            )
            known_terminal = known is not None and CallStatus(known.status) in TERMINAL_STATUSES
        if known_terminal or (known is None and not adoptable):
            # Applied without trust: it can only be recorded as duplicate/stale/unmatched.
            with session_factory() as session:
                result = apply_execution(
                    session,
                    settings,
                    provider_name,
                    provider_agent_id(provider, settings),
                    ProviderExecution(provider_call_id, status, provider_status),
                    trusted=False,
                    event_id=event_id,
                )
            with session_factory() as session, transaction(session):
                _succeed(_owned_job(session, job, worker_id))
            return result

        # Terminal states carry the decision: fetch the authoritative execution from the
        # provider API instead of trusting the webhook body.
        try:
            execution = provider.get_execution(provider_call_id)
            trusted = True
        except VoiceProviderError as exc:
            with session_factory() as session, transaction(session):
                if exc.kind is ErrorKind.NOT_FOUND:
                    _finish(
                        session,
                        job,
                        worker_id,
                        event_id,
                        EventStatus.IGNORED,
                        "not_found_at_provider",
                    )
                    return "ignored"
                row = _owned_job(session, job, worker_id)
                if not _retry_or_fail(row, settings, exc.category):
                    event = _require(
                        session.get(VoiceProviderEvent, event_id, with_for_update=True)
                    )
                    event.status = EventStatus.FAILED
                    event.result = exc.category[:48]
                    return "failed"
            return "retry"
    else:
        # Lifecycle-only update; can never create a vote or a terminal state.
        execution = ProviderExecution(provider_call_id, status, provider_status)

    with session_factory() as session:
        result = apply_execution(
            session,
            settings,
            provider_name,
            provider_agent_id(provider, settings),
            execution,
            trusted=trusted,
            event_id=event_id,
        )
    with session_factory() as session, transaction(session):
        _succeed(_owned_job(session, job, worker_id))
    return result


def _finish(
    session: Session, job: ClaimedJob, worker_id: str, event_id: UUID, status: str, result: str
) -> None:
    event = _require(session.get(VoiceProviderEvent, event_id, with_for_update=True))
    event.status = status
    event.result = result
    event.processed_at = utcnow()
    _succeed(_owned_job(session, job, worker_id))


# --- reconciliation -----------------------------------------------------------------


def schedule_reconciliations(
    session: Session, settings: Settings, now: datetime | None = None
) -> int:
    """Enqueue a provider poll for calls with no update for a while (missed webhooks)."""
    now = now or utcnow()
    cutoff = now - timedelta(seconds=settings.voice_reconcile_after_seconds)
    with transaction(session):
        stale = session.scalars(
            select(VoiceCall.id).where(
                VoiceCall.status.in_(ACTIVE_CALL_STATUSES),
                VoiceCall.provider_call_id.is_not(None),
                VoiceCall.last_event_at < cutoff,
            )
        ).all()
        created = 0
        for call_id in stale:
            inserted = session.execute(
                insert(VoiceJob)
                .values(
                    job_type=JobType.RECONCILE_CALL,
                    voice_call_id=call_id,
                    status=JobStatus.PENDING,
                    max_attempts=settings.voice_job_max_attempts,
                )
                .on_conflict_do_nothing()
                .returning(VoiceJob.id)
            ).scalar()
            created += inserted is not None
    return created


def run_reconcile(
    session_factory: sessionmaker[Session],
    provider: VoiceProvider,
    settings: Settings,
    job: ClaimedJob,
    worker_id: str,
) -> str:
    with session_factory() as session:
        call = _require(session.get(VoiceCall, job.voice_call_id))
        provider_name, provider_call_id = call.provider, call.provider_call_id
        started = call.initiated_at or call.created_at
    timed_out = utcnow() - started > timedelta(seconds=settings.voice_call_timeout_seconds)

    result = "polled"
    try:
        execution = provider.get_execution(provider_call_id or "")
        with session_factory() as session:
            result = apply_execution(
                session,
                settings,
                provider_name,
                provider_agent_id(provider, settings),
                execution,
                trusted=True,
            )
    except VoiceProviderError as exc:
        if not timed_out and exc.kind is not ErrorKind.NOT_FOUND:
            with session_factory() as session, transaction(session):
                _retry_or_fail(_owned_job(session, job, worker_id), settings, exc.category)
            return "retry"
        result = exc.category

    with session_factory() as session, transaction(session):
        row = _owned_job(session, job, worker_id)
        call = session.get(VoiceCall, job.voice_call_id, with_for_update=True)
        if call is not None and CallStatus(call.status) not in TERMINAL_STATUSES:
            if timed_out:
                _end_call(session, call, CallStatus.TIMED_OUT, "no_final_result")
                result = "timed_out"
            else:
                call.last_event_at = utcnow()  # polled; wait another interval
        _succeed(row)
    return result


# --- request jobs: deadline expiry and escalation rounds ----------------------------
#
# Each runs entirely inside ONE transaction that holds the job row and then the request
# row, so the business transition, its audit events and the job's completion or
# rescheduling commit together. Lock order: job, then request (no other path locks a
# request job row after a request row).


def _finish_request_job(row: VoiceJob, next_run_at: datetime | None) -> None:
    if next_run_at is None:
        _succeed(row)
    else:
        row.status = JobStatus.PENDING
        row.lease_expires_at = None
        row.locked_by = None
        row.attempts = 0  # a reschedule is progress, not a failed attempt
        row.run_after = next_run_at


def run_expire(session_factory, provider, settings, job, worker_id, *, now=None) -> str:
    from app.services.approval_service import expire_if_due

    with session_factory() as session, transaction(session):
        row = _owned_job(session, job, worker_id)
        if row is None:
            return "lease_lost"
        result, reschedule = expire_if_due(session, _require(job.request_id), now or utcnow())
        _finish_request_job(row, reschedule)
    logger.info(
        "workflow.expire_checked", extra={"request_id": str(job.request_id), "result": result}
    )
    return result


def run_escalate(session_factory, provider, settings, job, worker_id, *, now=None) -> str:
    from app.services.escalation_service import run_escalation_round

    with session_factory() as session, transaction(session):
        row = _owned_job(session, job, worker_id)
        if row is None:
            return "lease_lost"
        result, next_run = run_escalation_round(
            session, settings, _require(job.request_id), now or utcnow()
        )
        _finish_request_job(row, next_run)
    logger.info(
        "workflow.escalation_round", extra={"request_id": str(job.request_id), "result": result}
    )
    return result


def _voice_unavailable(session_factory, job: ClaimedJob, worker_id: str) -> str:
    """A voice job exists but no provider is configured: fail it, never pretend."""
    with session_factory() as session, transaction(session):
        row = _owned_job(session, job, worker_id)
        if job.job_type == JobType.INITIATE_CALL and job.voice_call_id is not None:
            call = session.get(VoiceCall, job.voice_call_id, with_for_update=True)
            if call is not None and call.status == CallStatus.QUEUED.value:
                _end_call(session, call, CallStatus.FAILED, "voice_unavailable")
        _fail(row, "voice_unavailable")
    return "voice_unavailable"


HANDLERS = {
    JobType.INITIATE_CALL: run_initiate,
    JobType.PROCESS_EVENT: run_process_event,
    JobType.RECONCILE_CALL: run_reconcile,
}
REQUEST_HANDLERS = {
    JobType.EXPIRE_REQUEST: run_expire,
    JobType.ESCALATE_REQUEST: run_escalate,
}


def run_pending_jobs(
    session_factory: sessionmaker[Session],
    provider: VoiceProvider | None,
    settings: Settings,
    worker_id: str,
    *,
    max_jobs: int = 100,
    now: datetime | None = None,
) -> list[tuple[str, str]]:
    """Claim and run jobs until none is due (or ``max_jobs``). Returns (type, result).

    ``provider`` may be None when voice is disabled: request jobs (deadlines, escalation
    rounds) still run; voice jobs fail with ``voice_unavailable``.
    """
    results: list[tuple[str, str]] = []
    for _ in range(max_jobs):
        with session_factory() as session:
            job = claim_next(session, settings, worker_id, now)
        if job is None:
            break
        try:
            if job.job_type in REQUEST_HANDLERS:
                result = REQUEST_HANDLERS[job.job_type](
                    session_factory, provider, settings, job, worker_id, now=now
                )
            elif provider is None:
                result = _voice_unavailable(session_factory, job, worker_id)
            else:
                result = HANDLERS[job.job_type](session_factory, provider, settings, job, worker_id)
        except Exception:
            # Unexpected bug: release the job for a later retry rather than losing it.
            logger.exception("voice.job_crashed", extra={"job_id": str(job.id)})
            with session_factory() as session, transaction(session):
                _retry_or_fail(_owned_job(session, job, worker_id), settings, "internal_error")
            result = "internal_error"
        results.append((job.job_type, result))
    return results
