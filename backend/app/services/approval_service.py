"""Approval requests and the single, transaction-safe decision workflow.

Every path that can change a request's status goes through this module: votes (API or
voice), conditions, condition resolution and deadline expiry. Each runs in one
transaction that locks the request row FOR UPDATE, re-evaluates the request with the
pure engine in app/domain/approval_rules.py, applies only a legal transition and writes
the audit events alongside. Nothing here talks to the network.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.errors import BusinessRuleError, ConflictError, NotFoundError, UnprocessableError
from app.db.base import utcnow
from app.db.models import (
    ApprovalCondition,
    ApprovalRequest,
    ApprovalVote,
    Approver,
    EscalationStep,
    RequestApprover,
    VoiceJob,
)
from app.db.models.voice import JobType
from app.db.models.workflow import ConditionStatus
from app.db.session import transaction
from app.domain.approval_rules import (
    ApprovalMode,
    Ballot,
    Evaluation,
    RequestStatus,
    VetoMode,
    VoteChoice,
    evaluate_request,
)
from app.domain.snapshot import RequestRules, build_snapshot, read_snapshot
from app.schemas.request import ApprovalRequestCreate
from app.services import audit_service
from app.services.audit_service import AuditEventType
from app.services.policy_service import get_policy

IDEMPOTENCY_UNIQUE_INDEX = "uq_approval_requests_organization_id_idempotency_key_hash"
VOTE_UNIQUE_CONSTRAINT = "uq_approval_votes_request_id_approver_id"
SEAT_UNIQUE_CONSTRAINT = "uq_approval_votes_request_id_seat_approver_id"
# Request jobs are retried a few times on unexpected errors; they are idempotent.
REQUEST_JOB_MAX_ATTEMPTS = 5

_FINAL_EVENT = {
    RequestStatus.APPROVED: AuditEventType.REQUEST_APPROVED,
    RequestStatus.REJECTED: AuditEventType.REQUEST_REJECTED,
    RequestStatus.EXPIRED: AuditEventType.REQUEST_EXPIRED,
}


@dataclass(frozen=True)
class VoteOutcome:
    vote: ApprovalVote
    request: ApprovalRequest
    created: bool


@dataclass(frozen=True)
class ConditionOutcome:
    condition: ApprovalCondition
    request: ApprovalRequest
    created: bool
    # Approver to call again for an unconditional re-approval (condition satisfied).
    reapproval_approver_id: UUID | None = None


def request_rules(request: ApprovalRequest) -> RequestRules:
    return read_snapshot(request.policy_snapshot)


# --- Creation ------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_fingerprint(data: ApprovalRequestCreate) -> str:
    """Canonical hash of what the client asked for (used to detect a reused key)."""
    canonical = json.dumps(
        {
            "policy_id": str(data.policy_id),
            "action": data.action,
            "summary": data.summary,
            "context": data.context,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return _sha256(canonical)


def _find_by_idempotency_key(
    session: Session, organization_id: UUID, key_hash: str
) -> tuple[UUID, str] | None:
    """(request id, payload fingerprint) for a key, as plain values."""
    row = session.execute(
        select(ApprovalRequest.id, ApprovalRequest.idempotency_fingerprint).where(
            ApprovalRequest.organization_id == organization_id,
            ApprovalRequest.idempotency_key_hash == key_hash,
        )
    ).first()
    session.rollback()  # end the read; the caller decides what to do next
    return (row[0], row[1]) if row else None


def _replay(existing: tuple[UUID, str], fingerprint: str) -> UUID:
    request_id, stored = existing
    if stored != fingerprint:
        raise UnprocessableError(
            "This Idempotency-Key was already used for a different request.",
            code="idempotency_key_reused",
        )
    return request_id


def submit_request(
    session: Session,
    organization_id: UUID,
    data: ApprovalRequestCreate,
    *,
    idempotency_key: str | None = None,
) -> tuple[ApprovalRequest, bool]:
    """Create a request, or replay the one created earlier with the same key.

    Returns (request, created). With a key, any number of retries (sequential or
    concurrent) with the same payload yield one request; the same key with a different
    payload is refused with 422. Keys are scoped to the organization and stored only as
    a SHA-256 hash. Without a key, every call creates a new request (unchanged).
    """
    if idempotency_key is None:
        return create_request(session, organization_id, data), True
    key_hash = _sha256(idempotency_key)
    fingerprint = request_fingerprint(data)
    existing = _find_by_idempotency_key(session, organization_id, key_hash)
    if existing is not None:
        return get_request(session, organization_id, _replay(existing, fingerprint)), False
    try:
        request = create_request(
            session, organization_id, data, idempotency=(key_hash, fingerprint)
        )
    except IntegrityError as exc:
        # A concurrent call with the same key won the race. Its transaction committed;
        # ours rolled back entirely (request, snapshot rows, jobs and audit event).
        if not _is_unique_violation(exc, IDEMPOTENCY_UNIQUE_INDEX):
            raise
        existing = _find_by_idempotency_key(session, organization_id, key_hash)
        if existing is None:  # pragma: no cover - the winner committed before we failed
            raise
        return get_request(session, organization_id, _replay(existing, fingerprint)), False
    return request, True


def create_request(
    session: Session,
    organization_id: UUID,
    data: ApprovalRequestCreate,
    *,
    idempotency: tuple[str, str] | None = None,
) -> ApprovalRequest:
    now = utcnow()
    with transaction(session):
        # FOR SHARE serialises against concurrent policy updates, so the snapshot always
        # reflects one consistent policy version.
        policy = get_policy(session, organization_id, data.policy_id, lock="share")
        if not policy.is_active:
            raise BusinessRuleError("The policy is inactive.", code="policy_inactive")
        if any(not a.is_active for a in policy.approvers):
            raise BusinessRuleError(
                "The policy has an inactive approver and cannot accept new requests.",
                code="policy_approver_inactive",
            )

        primaries = [(link.approver_id, link.can_veto) for link in policy.primary_links]
        backups = [link.approver_id for link in policy.backup_links if link.approver.is_active]
        snapshot = build_snapshot(
            policy_id=policy.id,
            name=policy.name,
            version=policy.version,
            mode=ApprovalMode(policy.approval_mode),
            required_approvals=policy.required_approvals,
            veto_mode=VetoMode(policy.veto_mode),
            primaries=primaries,
            backups=backups,
            deadline_seconds=policy.decision_deadline_seconds,
            escalation_after_seconds=policy.escalation_after_seconds if backups else None,
            max_escalations=min(policy.max_escalations, len(backups)) if backups else 0,
        )
        expires_at = (
            now + timedelta(seconds=policy.decision_deadline_seconds)
            if policy.decision_deadline_seconds
            else None
        )
        request = ApprovalRequest(
            organization_id=organization_id,
            policy_id=policy.id,
            policy_version=policy.version,
            action=data.action,
            summary=data.summary,
            context=data.context,
            policy_snapshot=snapshot,
            status=RequestStatus.PENDING.value,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
            idempotency_key_hash=idempotency[0] if idempotency else None,
            idempotency_fingerprint=idempotency[1] if idempotency else None,
        )
        session.add(request)
        session.flush()

        rows = [(approver_id, "primary", can_veto) for approver_id, can_veto in primaries]
        rows += [(approver_id, "backup", False) for approver_id in backups]
        for position, (approver_id, role, can_veto) in enumerate(rows):
            session.add(
                RequestApprover(
                    request_id=request.id,
                    approver_id=approver_id,
                    organization_id=organization_id,
                    role=role,
                    can_veto=can_veto,
                    position=position,
                )
            )

        if expires_at is not None:
            session.add(
                VoiceJob(
                    job_type=JobType.EXPIRE_REQUEST,
                    request_id=request.id,
                    run_after=expires_at,
                    max_attempts=REQUEST_JOB_MAX_ATTEMPTS,
                )
            )
        if snapshot["escalation_after_seconds"]:
            session.add(
                VoiceJob(
                    job_type=JobType.ESCALATE_REQUEST,
                    request_id=request.id,
                    run_after=now + timedelta(seconds=snapshot["escalation_after_seconds"]),
                    max_attempts=REQUEST_JOB_MAX_ATTEMPTS,
                )
            )
        audit_service.record_event(
            session,
            organization_id=organization_id,
            request_id=request.id,
            event_type=AuditEventType.REQUEST_CREATED,
            data={
                "policy_id": str(policy.id),
                "policy_version": policy.version,
                "approval_mode": snapshot["approval_mode"],
                "required_approvals": policy.required_approvals,
                "veto_mode": snapshot["veto_mode"],
                "approver_ids": snapshot["approver_ids"],
                "veto_approver_ids": [str(a) for a, can_veto in primaries if can_veto],
                "backup_approver_ids": snapshot["backups"],
                "expires_at": expires_at.isoformat() if expires_at else None,
                "escalation_after_seconds": snapshot["escalation_after_seconds"],
                "max_escalations": snapshot["max_escalations"],
                "action": data.action,
            },
        )
        request_id = request.id
    return get_request(session, organization_id, request_id)


# --- Reads ---------------------------------------------------------------------------


def get_request(session: Session, organization_id: UUID, request_id: UUID) -> ApprovalRequest:
    request = session.scalar(
        select(ApprovalRequest)
        .where(ApprovalRequest.id == request_id, ApprovalRequest.organization_id == organization_id)
        .options(selectinload(ApprovalRequest.votes))
        .execution_options(populate_existing=True)
    )
    if request is None:
        raise NotFoundError("Approval request not found.", code="request_not_found")
    return request


def list_requests(
    session: Session,
    organization_id: UUID,
    *,
    limit: int,
    offset: int,
    status: RequestStatus | None = None,
) -> tuple[list[ApprovalRequest], int]:
    conditions = [ApprovalRequest.organization_id == organization_id]
    if status is not None:
        conditions.append(ApprovalRequest.status == status.value)
    total = (
        session.scalar(select(func.count()).select_from(ApprovalRequest).where(*conditions)) or 0
    )
    items = session.scalars(
        select(ApprovalRequest)
        .where(*conditions)
        .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(items), total


def list_request_approvers(
    session: Session, request: ApprovalRequest
) -> list[tuple[RequestApprover, str]]:
    """The request's snapshot approvers (primaries, then backups) with display names."""
    rows = session.execute(
        select(RequestApprover, Approver.name)
        .join(
            Approver,
            (Approver.id == RequestApprover.approver_id)
            & (Approver.organization_id == RequestApprover.organization_id),
        )
        .where(
            RequestApprover.request_id == request.id,
            RequestApprover.organization_id == request.organization_id,
        )
        .order_by(RequestApprover.position)
    ).all()
    return [(row[0], row[1]) for row in rows]


@dataclass(frozen=True)
class RequestStats:
    total: int
    by_status: dict[str, int]
    waiting_for_condition: int
    awaiting_reapproval: int
    deadline_passed: int
    pending_evaluated: int
    pending_total: int


# Evaluation-derived dashboard counts look at no more than this many pending requests.
STATS_PENDING_EVALUATION_LIMIT = 200


def request_stats(session: Session, organization_id: UUID) -> RequestStats:
    counts = dict(
        session.execute(
            select(ApprovalRequest.status, func.count())
            .where(ApprovalRequest.organization_id == organization_id)
            .group_by(ApprovalRequest.status)
        ).all()
    )
    by_status = {status.value: int(counts.get(status.value, 0)) for status in RequestStatus}
    pending = session.scalars(
        select(ApprovalRequest)
        .where(
            ApprovalRequest.organization_id == organization_id,
            ApprovalRequest.status == RequestStatus.PENDING.value,
        )
        .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
        .limit(STATS_PENDING_EVALUATION_LIMIT)
    ).all()
    now = utcnow()
    condition = reapproval = deadline = 0
    for request in pending:
        # The same engine as everywhere else; the dashboard never computes decisions.
        evaluation = evaluate(session, request, now)
        if evaluation.deadline_passed:
            deadline += 1
        elif evaluation.condition_seats:
            condition += 1
        elif evaluation.reapproval_seats:
            reapproval += 1
    return RequestStats(
        total=sum(by_status.values()),
        by_status=by_status,
        waiting_for_condition=condition,
        awaiting_reapproval=reapproval,
        deadline_passed=deadline,
        pending_evaluated=len(pending),
        pending_total=by_status[RequestStatus.PENDING.value],
    )


def list_conditions(
    session: Session, organization_id: UUID, request_id: UUID
) -> list[ApprovalCondition]:
    get_request(session, organization_id, request_id)
    return list(
        session.scalars(
            select(ApprovalCondition)
            .where(
                ApprovalCondition.request_id == request_id,
                ApprovalCondition.organization_id == organization_id,
            )
            .order_by(ApprovalCondition.created_at, ApprovalCondition.id)
        )
    )


def list_escalations(
    session: Session, organization_id: UUID, request_id: UUID
) -> list[EscalationStep]:
    get_request(session, organization_id, request_id)
    return list(
        session.scalars(
            select(EscalationStep)
            .where(
                EscalationStep.request_id == request_id,
                EscalationStep.organization_id == organization_id,
            )
            .order_by(EscalationStep.sequence)
        )
    )


def _deadline_passed(request: ApprovalRequest, now: datetime) -> bool:
    return request.expires_at is not None and now >= request.expires_at


def _awaits_reapproval(condition: ApprovalCondition, seat_vote: ApprovalVote | None) -> bool:
    """Whether a satisfied condition still blocks approval of its seat.

    Only the approver who stated the condition can clear it, by approving after it was
    resolved. A rejection of the seat (by any holder) decides the seat against approval,
    so the condition no longer has anything to block. Anything else keeps blocking, so
    an approval by another holder of the seat (refused since the fix, but possible in
    older data) never counts as the re-approval.
    """
    if condition.status != ConditionStatus.SATISFIED:
        return False
    if seat_vote is None:
        return True
    if seat_vote.vote == VoteChoice.REJECT.value:
        return False
    return not (
        seat_vote.approver_id == condition.approver_id
        and seat_vote.created_at >= condition.closed_at
    )


def evaluate(session: Session, request: ApprovalRequest, now: datetime | None = None) -> Evaluation:
    """Server-side evaluation from persisted, validated state only."""
    now = now or utcnow()
    rules = request_rules(request)
    votes = session.scalars(
        select(ApprovalVote)
        .where(ApprovalVote.request_id == request.id)
        .order_by(ApprovalVote.created_at, ApprovalVote.id)
    ).all()
    conditions = session.scalars(
        select(ApprovalCondition).where(ApprovalCondition.request_id == request.id)
    ).all()
    open_seats = {c.seat_approver_id for c in conditions if c.status == ConditionStatus.OPEN}
    seat_votes = {v.seat_approver_id: v for v in votes}
    reapproval_by = {
        c.seat_approver_id: c.approver_id
        for c in conditions
        if _awaits_reapproval(c, seat_votes.get(c.seat_approver_id))
    }
    return evaluate_request(
        rules.rules,
        [Ballot(v.approver_id, v.seat_approver_id, VoteChoice(v.vote)) for v in votes],
        open_seats,
        set(reapproval_by),
        reapproval_by=reapproval_by,
        deadline_passed=_deadline_passed(request, now),
        current=RequestStatus(request.status),
    )


def evaluation_data(evaluation: Evaluation) -> dict[str, Any]:
    return {
        "state": evaluation.state.value,
        "reason": evaluation.reason.value,
        "approvals": evaluation.approvals,
        "rejections": evaluation.rejections,
        "required_approvals": evaluation.required_approvals,
        "veto_triggered": evaluation.veto_triggered,
        "outstanding_approver_ids": [str(s) for s in evaluation.outstanding_seats],
        "condition_approver_ids": [str(s) for s in evaluation.condition_seats],
        "reapproval_approver_ids": [str(a) for a in evaluation.reapproval_approvers],
        "reapproval_seat_ids": [str(s) for s in evaluation.reapproval_seats],
    }


# --- Shared transition logic ------------------------------------------------------------


def _lock_request(session: Session, organization_id: UUID, request_id: UUID) -> ApprovalRequest:
    request = session.scalar(
        select(ApprovalRequest)
        .where(ApprovalRequest.id == request_id, ApprovalRequest.organization_id == organization_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if request is None:
        raise NotFoundError("Approval request not found.", code="request_not_found")
    return request


def require_open(request: ApprovalRequest, now: datetime) -> None:
    status = RequestStatus(request.status)
    if status is RequestStatus.EXPIRED or (
        status is RequestStatus.PENDING and _deadline_passed(request, now)
    ):
        raise ConflictError("The request's deadline has passed.", code="request_expired")
    if status is not RequestStatus.PENDING:
        raise ConflictError(
            f"The request has already been {request.status}.", code="request_already_decided"
        )


def _seat_for(session: Session, request: ApprovalRequest, approver_id: UUID) -> UUID | None:
    """The seat an approver may decide: their own (primary) or one escalated to them."""
    if approver_id in request.authorized_approver_ids:
        return approver_id
    return session.scalar(
        select(EscalationStep.seat_approver_id).where(
            EscalationStep.request_id == request.id,
            EscalationStep.backup_approver_id == approver_id,
        )
    )


def _pending_reapprover(session: Session, request: ApprovalRequest, seat: UUID) -> UUID | None:
    """The approver whose satisfied condition on an undecided seat awaits re-approval."""
    return session.scalar(
        select(ApprovalCondition.approver_id)
        .where(
            ApprovalCondition.request_id == request.id,
            ApprovalCondition.seat_approver_id == seat,
            ApprovalCondition.status == ConditionStatus.SATISFIED,
        )
        .order_by(ApprovalCondition.closed_at.desc())
        .limit(1)
    )


def _apply_evaluation(
    session: Session, request: ApprovalRequest, now: datetime, cause: dict[str, Any]
) -> Evaluation:
    """Re-evaluate a locked request and apply a final status if one is reached."""
    evaluation = evaluate(session, request, now)
    request.updated_at = now
    if request.status == RequestStatus.PENDING.value and evaluation.status is not (
        RequestStatus.PENDING
    ):
        request.status = evaluation.status.value
        request.decided_at = now
        audit_service.record_event(
            session,
            organization_id=request.organization_id,
            request_id=request.id,
            event_type=_FINAL_EVENT[evaluation.status],
            data={
                **evaluation_data(evaluation),
                # Chunk 1 field names, kept for existing consumers of the audit data.
                "approve_votes": evaluation.approvals,
                "reject_votes": evaluation.rejections,
                **cause,
            },
        )
        _lapse_open_conditions(session, request, now)
    session.flush()
    return evaluation


def _lapse_open_conditions(session: Session, request: ApprovalRequest, now: datetime) -> None:
    for condition in session.scalars(
        select(ApprovalCondition).where(
            ApprovalCondition.request_id == request.id,
            ApprovalCondition.status == ConditionStatus.OPEN,
        )
    ):
        condition.status = ConditionStatus.LAPSED
        condition.closed_at = now
        audit_service.record_event(
            session,
            organization_id=request.organization_id,
            request_id=request.id,
            event_type=AuditEventType.CONDITION_LAPSED,
            data={"condition_id": str(condition.id), "request_status": request.status},
        )


def _is_unique_violation(exc: IntegrityError, *names: str) -> bool:
    constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    return constraint in names


# --- Votes -----------------------------------------------------------------------------


def cast_vote(
    session: Session,
    organization_id: UUID,
    request_id: UUID,
    approver_id: UUID,
    choice: VoteChoice,
) -> VoteOutcome:
    """Record one approver's vote and apply the resulting decision atomically.

    The request row is locked FOR UPDATE, so concurrent votes on one request are applied
    one after another; unique constraints on (request, approver) and (request, seat) back
    this up. The vote, the status change and their audit events commit together.
    """
    try:
        with transaction(session):
            return record_vote(session, organization_id, request_id, approver_id, choice)
    except IntegrityError as exc:
        if _is_unique_violation(exc, VOTE_UNIQUE_CONSTRAINT, SEAT_UNIQUE_CONSTRAINT):
            raise ConflictError(
                "A vote for this approver or seat was recorded concurrently.",
                code="vote_conflict",
            ) from exc
        raise


def record_vote(
    session: Session,
    organization_id: UUID,
    request_id: UUID,
    approver_id: UUID,
    choice: VoteChoice,
    *,
    source: str = "api",
    source_details: dict[str, str] | None = None,
    now: datetime | None = None,
) -> VoteOutcome:
    """Validate and apply a vote inside the caller's open transaction (no commit).

    Every rejection (AppError) is raised before anything is written, so a caller may
    catch it and continue its own transaction. Callers that need to survive an
    IntegrityError should wrap this in a savepoint (``session.begin_nested()``).
    """
    request = _lock_request(session, organization_id, request_id)
    # Read the clock only once the lock is held: a vote that waited for the lock until
    # after the deadline must be judged at that later moment.
    now = now or utcnow()

    # Authorization comes from the snapshot (and persisted escalation steps), never the
    # live policy.
    seat = _seat_for(session, request, approver_id)
    if seat is None:
        raise BusinessRuleError(
            "This approver is not authorized to vote on this request.",
            code="approver_not_authorized",
        )

    existing = session.scalar(
        select(ApprovalVote).where(
            ApprovalVote.request_id == request.id, ApprovalVote.approver_id == approver_id
        )
    )
    if existing is not None:
        if existing.vote == choice.value:
            return VoteOutcome(existing, request, created=False)
        raise ConflictError(
            "This approver has already cast a different vote.", code="vote_conflict"
        )

    require_open(request, now)

    if session.scalar(
        select(ApprovalVote.id).where(
            ApprovalVote.request_id == request.id, ApprovalVote.seat_approver_id == seat
        )
    ):
        raise ConflictError(
            "This approver's seat has already been decided by another approver.",
            code="seat_already_decided",
        )

    open_conditions = session.scalars(
        select(ApprovalCondition)
        .where(
            ApprovalCondition.request_id == request.id,
            ApprovalCondition.seat_approver_id == seat,
            ApprovalCondition.status == ConditionStatus.OPEN,
        )
        .with_for_update()
    ).all()
    if open_conditions and choice is VoteChoice.APPROVE:
        raise ConflictError(
            "A condition on this seat is still open; resolve it before approving.",
            code="condition_open",
        )
    if choice is VoteChoice.APPROVE:
        # A satisfied condition is cleared only by its own approver's fresh approval; any
        # other holder of the seat approving would decide the seat without that, leaving
        # the request blocked for good. Rejections stay allowed.
        reapprover = _pending_reapprover(session, request, seat)
        if reapprover is not None and reapprover != approver_id:
            raise ConflictError(
                "This seat is waiting for the approver who set a condition to re-approve.",
                code="reapproval_required",
            )

    approver = session.scalar(
        select(Approver)
        .where(Approver.id == approver_id, Approver.organization_id == organization_id)
        # FOR SHARE: the approver cannot be deactivated between this check and commit.
        .with_for_update(read=True)
    )
    if approver is None or not approver.is_active:
        raise BusinessRuleError(
            "This approver is inactive and cannot vote.", code="approver_inactive"
        )

    # --- all checks passed; write ---
    for condition in open_conditions:  # an outright rejection supersedes a condition
        condition.status = ConditionStatus.SUPERSEDED
        condition.closed_at = now
        audit_service.record_event(
            session,
            organization_id=organization_id,
            request_id=request.id,
            event_type=AuditEventType.CONDITION_SUPERSEDED,
            data={"condition_id": str(condition.id), "by_approver_id": str(approver_id)},
        )

    vote = ApprovalVote(
        organization_id=organization_id,
        request_id=request.id,
        approver_id=approver_id,
        seat_approver_id=seat,
        vote=choice.value,
        created_at=now,
    )
    session.add(vote)
    session.flush()
    progress = evaluate(session, request, now)
    audit_service.record_event(
        session,
        organization_id=organization_id,
        request_id=request.id,
        event_type=AuditEventType.VOTE_RECORDED,
        data={
            "vote_id": str(vote.id),
            "approver_id": str(approver_id),
            "seat_approver_id": str(seat),
            "vote": vote.vote,
            "source": source,
            "approvals": progress.approvals,
            "required_approvals": progress.required_approvals,
            **(source_details or {}),
        },
    )
    _apply_evaluation(session, request, now, {"decisive_vote_id": str(vote.id)})
    return VoteOutcome(vote, request, created=True)


# --- Conditions --------------------------------------------------------------------------


def record_condition(
    session: Session,
    organization_id: UUID,
    request_id: UUID,
    approver_id: UUID,
    description: str,
    *,
    source: str,
    voice_call_id: UUID | None = None,
    now: datetime | None = None,
) -> ConditionOutcome:
    """Record a conditional approval inside the caller's transaction (no commit).

    It is never a vote: the seat stays undecided and the request cannot be approved while
    the condition is open. Errors are raised before anything is written.
    """
    description = " ".join(description.split())[:500]
    if not description:
        raise BusinessRuleError("A condition needs a description.", code="condition_invalid")
    request = _lock_request(session, organization_id, request_id)
    now = now or utcnow()  # after the lock, like votes
    seat = _seat_for(session, request, approver_id)
    if seat is None:
        raise BusinessRuleError(
            "This approver is not authorized to decide this request.",
            code="approver_not_authorized",
        )
    existing = session.scalar(
        select(ApprovalCondition).where(
            ApprovalCondition.request_id == request.id,
            ApprovalCondition.approver_id == approver_id,
            ApprovalCondition.status == ConditionStatus.OPEN,
        )
    )
    if existing is not None:
        if existing.description == description:
            return ConditionOutcome(existing, request, created=False)
        raise ConflictError(
            "This approver already has a different open condition.", code="condition_conflict"
        )
    require_open(request, now)
    if session.scalar(
        select(ApprovalVote.id).where(
            ApprovalVote.request_id == request.id, ApprovalVote.seat_approver_id == seat
        )
    ):
        raise ConflictError(
            "This seat has already been decided by a vote.", code="seat_already_decided"
        )
    # One approver at a time may hold conditions on a seat; otherwise two holders could
    # each wait for the other's re-approval and the seat could never be approved.
    if session.scalar(
        select(ApprovalCondition.id).where(
            ApprovalCondition.request_id == request.id,
            ApprovalCondition.seat_approver_id == seat,
            ApprovalCondition.approver_id != approver_id,
            ApprovalCondition.status.in_((ConditionStatus.OPEN, ConditionStatus.SATISFIED)),
        )
    ):
        raise ConflictError(
            "Another approver for this seat has a condition that is not yet settled.",
            code="seat_condition_pending",
        )
    approver = session.get(Approver, approver_id)
    if approver is None or not approver.is_active:
        raise BusinessRuleError("This approver is inactive.", code="approver_inactive")

    condition = ApprovalCondition(
        organization_id=organization_id,
        request_id=request.id,
        approver_id=approver_id,
        seat_approver_id=seat,
        policy_version=request.policy_version,
        source=source,
        voice_call_id=voice_call_id,
        description=description,
        status=ConditionStatus.OPEN,
        created_at=now,
    )
    session.add(condition)
    session.flush()
    audit_service.record_event(
        session,
        organization_id=organization_id,
        request_id=request.id,
        event_type=AuditEventType.CONDITION_CREATED,
        data={
            "condition_id": str(condition.id),
            "approver_id": str(approver_id),
            "seat_approver_id": str(seat),
            "source": source,
            "voice_call_id": str(voice_call_id) if voice_call_id else None,
            "policy_version": request.policy_version,
        },
    )
    _apply_evaluation(session, request, now, {"condition_id": str(condition.id)})
    return ConditionOutcome(condition, request, created=True)


def create_condition(
    session: Session,
    organization_id: UUID,
    request_id: UUID,
    approver_id: UUID,
    description: str,
) -> ConditionOutcome:
    """Trusted-integration entry point (same trust level as the vote endpoint)."""
    with transaction(session):
        return record_condition(
            session, organization_id, request_id, approver_id, description, source="api"
        )


def resolve_condition(
    session: Session,
    organization_id: UUID,
    request_id: UUID,
    condition_id: UUID,
    *,
    outcome: str,
    note: str,
    evidence_reference: str | None,
    actor_api_key_id: UUID,
    on_satisfied: Callable[[Session, ApprovalRequest, UUID], None] | None = None,
) -> ConditionOutcome:
    """An authorized actor records whether a condition was met.

    Resolution never approves: ``satisfied`` keeps the seat blocked until the approver
    re-approves unconditionally; ``not_satisfied`` voids the conditional approval and the
    seat is undecided again. Repeating the same resolution is idempotent; a different
    resolution of a closed condition is a conflict.
    """
    if outcome not in ConditionStatus.RESOLUTIONS:
        raise BusinessRuleError("Unknown resolution outcome.", code="condition_invalid")
    with transaction(session):
        request = _lock_request(session, organization_id, request_id)
        now = utcnow()  # after the lock, like votes
        condition = session.scalar(
            select(ApprovalCondition)
            .where(
                ApprovalCondition.id == condition_id,
                ApprovalCondition.request_id == request.id,
                ApprovalCondition.organization_id == organization_id,
            )
            .with_for_update()
        )
        if condition is None:
            raise NotFoundError("Condition not found.", code="condition_not_found")
        if condition.status != ConditionStatus.OPEN:
            if condition.status == outcome:
                return ConditionOutcome(condition, request, created=False)
            raise ConflictError(
                f"The condition is already {condition.status}.", code="condition_closed"
            )
        require_open(request, now)

        condition.status = outcome
        condition.closed_at = now
        condition.resolved_by_api_key_id = actor_api_key_id
        condition.resolution_note = note
        condition.evidence_reference = evidence_reference
        audit_service.record_event(
            session,
            organization_id=organization_id,
            request_id=request.id,
            event_type=AuditEventType.CONDITION_RESOLVED,
            data={
                "condition_id": str(condition.id),
                "outcome": outcome,
                "resolved_by_api_key_id": str(actor_api_key_id),
                "evidence_reference": evidence_reference,
            },
        )
        _apply_evaluation(session, request, now, {"condition_id": str(condition.id)})
        reapprove = condition.approver_id if outcome == ConditionStatus.SATISFIED else None
        if reapprove is not None and on_satisfied is not None:
            # Same transaction: the re-approval step is durable iff the resolution is.
            on_satisfied(session, request, reapprove)
        return ConditionOutcome(condition, request, created=True, reapproval_approver_id=reapprove)


# --- Deadline ---------------------------------------------------------------------------


def expire_if_due(
    session: Session, request_id: UUID, now: datetime | None = None
) -> tuple[str, datetime | None]:
    """Expire a pending request whose deadline has passed (inside caller's transaction).

    Returns (result, reschedule_at). Idempotent: final requests are left untouched, and
    a request that is not yet due asks to be checked again at its deadline.
    """
    now = now or utcnow()
    request = session.scalar(
        select(ApprovalRequest)
        .where(ApprovalRequest.id == request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if request is None:  # pragma: no cover - RESTRICT foreign key
        return "missing", None
    if request.status != RequestStatus.PENDING.value:
        return f"already_{request.status}", None
    if request.expires_at is None:
        return "no_deadline", None
    if now < request.expires_at:
        return "not_due", request.expires_at
    evaluation = _apply_evaluation(session, request, now, {"trigger": "deadline"})
    return f"now_{evaluation.status.value}", None
