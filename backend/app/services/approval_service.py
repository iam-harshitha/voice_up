"""Approval requests and the transaction-safe vote / state-transition workflow."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.errors import BusinessRuleError, ConflictError, NotFoundError
from app.db.base import utcnow
from app.db.models import ApprovalRequest, ApprovalVote, Approver
from app.db.session import transaction
from app.domain.approval_rules import (
    CastVote,
    IllegalTransitionError,
    RequestStatus,
    VoteChoice,
    next_status,
)
from app.schemas.request import ApprovalRequestCreate
from app.services import audit_service
from app.services.audit_service import AuditEventType
from app.services.policy_service import get_policy

VOTE_UNIQUE_CONSTRAINT = "uq_approval_votes_request_id_approver_id"


@dataclass(frozen=True)
class VoteOutcome:
    vote: ApprovalVote
    request: ApprovalRequest
    created: bool


def create_request(
    session: Session, organization_id: UUID, data: ApprovalRequestCreate
) -> ApprovalRequest:
    with transaction(session):
        # FOR SHARE serialises against concurrent policy updates, so the snapshot always
        # reflects one consistent policy version.
        policy = get_policy(session, organization_id, data.policy_id, lock="share")
        if not policy.is_active:
            raise BusinessRuleError("The policy is inactive.", code="policy_inactive")
        approvers = policy.approvers
        if any(not approver.is_active for approver in approvers):
            raise BusinessRuleError(
                "The policy has an inactive approver and cannot accept new requests.",
                code="policy_approver_inactive",
            )

        approver_ids = [str(approver.id) for approver in approvers]
        request = ApprovalRequest(
            organization_id=organization_id,
            policy_id=policy.id,
            policy_version=policy.version,
            action=data.action,
            summary=data.summary,
            context=data.context,
            policy_snapshot={
                "policy_id": str(policy.id),
                "name": policy.name,
                "version": policy.version,
                "required_approvals": policy.required_approvals,
                "approver_ids": approver_ids,
            },
            status=RequestStatus.PENDING.value,
        )
        session.add(request)
        session.flush()
        audit_service.record_event(
            session,
            organization_id=organization_id,
            request_id=request.id,
            event_type=AuditEventType.REQUEST_CREATED,
            data={
                "policy_id": str(policy.id),
                "policy_version": policy.version,
                "required_approvals": policy.required_approvals,
                "approver_ids": approver_ids,
                "action": data.action,
            },
        )
        request_id = request.id
    return get_request(session, organization_id, request_id)


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


def cast_vote(
    session: Session,
    organization_id: UUID,
    request_id: UUID,
    approver_id: UUID,
    choice: VoteChoice,
) -> VoteOutcome:
    """Record one approver's vote and apply the resulting decision atomically.

    The request row is locked FOR UPDATE, so concurrent votes on one request are applied
    one after another; the (request_id, approver_id) unique constraint backs this up.
    The vote, the status change and their audit events commit together or not at all.
    """
    try:
        with transaction(session):
            return record_vote(session, organization_id, request_id, approver_id, choice)
    except IntegrityError as exc:
        if _is_vote_unique_violation(exc):
            raise ConflictError(
                "A vote from this approver was recorded concurrently.", code="vote_conflict"
            ) from exc
        raise


def _is_vote_unique_violation(exc: IntegrityError) -> bool:
    constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    return constraint == VOTE_UNIQUE_CONSTRAINT


def record_vote(
    session: Session,
    organization_id: UUID,
    request_id: UUID,
    approver_id: UUID,
    choice: VoteChoice,
    *,
    source: str = "api",
    source_details: dict[str, str] | None = None,
) -> VoteOutcome:
    """Validate and apply a vote inside the caller's open transaction (no commit).

    Every rejection (AppError) is raised before anything is written, so a caller may
    catch it and continue its own transaction. Callers that need to survive an
    IntegrityError should wrap this in a savepoint (``session.begin_nested()``).
    """
    request = session.scalar(
        select(ApprovalRequest)
        .where(
            ApprovalRequest.id == request_id,
            ApprovalRequest.organization_id == organization_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if request is None:
        raise NotFoundError("Approval request not found.", code="request_not_found")

    # Authorization comes from the snapshot taken at creation, never the live policy.
    authorized = request.authorized_approver_ids
    if approver_id not in authorized:
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

    if request.status != RequestStatus.PENDING.value:
        raise ConflictError(
            f"The request has already been {request.status}.",
            code="request_already_decided",
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

    vote = ApprovalVote(
        organization_id=organization_id,
        request_id=request.id,
        approver_id=approver_id,
        vote=choice.value,
    )
    session.add(vote)
    session.flush()
    audit_service.record_event(
        session,
        organization_id=organization_id,
        request_id=request.id,
        event_type=AuditEventType.VOTE_RECORDED,
        data={
            "vote_id": str(vote.id),
            "approver_id": str(approver_id),
            "vote": vote.vote,
            "source": source,
            **(source_details or {}),
        },
    )

    all_votes = session.scalars(
        select(ApprovalVote).where(ApprovalVote.request_id == request.id)
    ).all()
    try:
        status = next_status(
            RequestStatus(request.status),
            request.required_approvals,
            [CastVote(v.approver_id, VoteChoice(v.vote)) for v in all_votes],
            authorized,
        )
    except IllegalTransitionError as exc:  # pragma: no cover - guarded above
        raise ConflictError(str(exc), code="request_already_decided") from exc

    now = utcnow()
    request.updated_at = now
    if status is not RequestStatus.PENDING:
        request.status = status.value
        request.decided_at = now
        audit_service.record_event(
            session,
            organization_id=organization_id,
            request_id=request.id,
            event_type=(
                AuditEventType.REQUEST_APPROVED
                if status is RequestStatus.APPROVED
                else AuditEventType.REQUEST_REJECTED
            ),
            data={
                "decisive_vote_id": str(vote.id),
                "approve_votes": sum(v.vote == VoteChoice.APPROVE for v in all_votes),
                "reject_votes": sum(v.vote == VoteChoice.REJECT for v in all_votes),
                "required_approvals": request.required_approvals,
            },
        )
    session.flush()
    return VoteOutcome(vote, request, created=True)
