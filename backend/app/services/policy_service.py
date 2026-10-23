"""Approvers and approval policies, always scoped to the caller's organization."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import BusinessRuleError, NotFoundError
from app.db.models import ApprovalPolicy, Approver, PolicyApprover
from app.db.session import transaction
from app.domain.approval_rules import validate_required_approvals
from app.schemas.policy import ApproverCreate, ApproverUpdate, PolicyCreate, PolicyUpdate

POLICY_INVALID = "approval_policy_invalid"


# --- Approvers ---------------------------------------------------------------------------


def create_approver(session: Session, organization_id: UUID, data: ApproverCreate) -> Approver:
    with transaction(session):
        approver = Approver(
            organization_id=organization_id,
            name=data.name,
            phone_number=data.phone_number,
            email=data.email,
        )
        session.add(approver)
        session.flush()
    return approver


def list_approvers(
    session: Session, organization_id: UUID, *, limit: int, offset: int
) -> tuple[list[Approver], int]:
    condition = Approver.organization_id == organization_id
    total = session.scalar(select(func.count()).select_from(Approver).where(condition)) or 0
    items = session.scalars(
        select(Approver)
        .where(condition)
        .order_by(Approver.created_at.desc(), Approver.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(items), total


def get_approver(
    session: Session, organization_id: UUID, approver_id: UUID, *, for_update: bool = False
) -> Approver:
    query = select(Approver).where(
        Approver.id == approver_id, Approver.organization_id == organization_id
    )
    if for_update:
        query = query.with_for_update()
    approver = session.scalar(query)
    if approver is None:
        raise NotFoundError("Approver not found.", code="approver_not_found")
    return approver


def update_approver(
    session: Session, organization_id: UUID, approver_id: UUID, data: ApproverUpdate
) -> Approver:
    with transaction(session):
        approver = get_approver(session, organization_id, approver_id, for_update=True)
        for field in data.model_fields_set:
            setattr(approver, field, getattr(data, field))
        if approver.phone_number is None and approver.email is None:
            raise BusinessRuleError(
                "An approver must keep at least one contact method.",
                code="approver_contact_required",
            )
    return approver


# --- Policies ----------------------------------------------------------------------------


def _resolve_assignment(
    session: Session,
    organization_id: UUID,
    required_approvals: int,
    approver_ids: Sequence[UUID],
) -> list[Approver]:
    """Validate an approver assignment and return the approvers in the given order."""
    validate_required_approvals(required_approvals)
    if len(set(approver_ids)) != len(approver_ids):
        raise BusinessRuleError("Assigned approvers must be distinct.", code=POLICY_INVALID)
    if len(approver_ids) != required_approvals:
        expected = (
            "exactly one authorized approver"
            if required_approvals == 1
            else "exactly two distinct authorized approvers"
        )
        raise BusinessRuleError(f"The policy must have {expected}.", code=POLICY_INVALID)

    # FOR SHARE: an approver cannot be deactivated while it is being assigned.
    found = {
        approver.id: approver
        for approver in session.scalars(
            select(Approver)
            .where(Approver.organization_id == organization_id, Approver.id.in_(approver_ids))
            .with_for_update(read=True)
        )
    }
    for approver_id in approver_ids:
        # Approvers of other organizations are reported exactly like missing ones.
        approver = found.get(approver_id)
        if approver is None:
            raise BusinessRuleError(f"Approver {approver_id} was not found.", code=POLICY_INVALID)
        if not approver.is_active:
            raise BusinessRuleError(f"Approver {approver_id} is inactive.", code=POLICY_INVALID)
    return [found[approver_id] for approver_id in approver_ids]


def _links(organization_id: UUID, approvers: Sequence[Approver]) -> list[PolicyApprover]:
    return [
        PolicyApprover(approver_id=approver.id, organization_id=organization_id, position=index)
        for index, approver in enumerate(approvers)
    ]


def _policy_query(organization_id: UUID, policy_id: UUID):
    return (
        select(ApprovalPolicy)
        .where(ApprovalPolicy.id == policy_id, ApprovalPolicy.organization_id == organization_id)
        .options(selectinload(ApprovalPolicy.approver_links))
    )


def get_policy(
    session: Session, organization_id: UUID, policy_id: UUID, *, lock: str | None = None
) -> ApprovalPolicy:
    """Load a policy owned by the organization. ``lock`` may be "update" or "share"."""
    query = _policy_query(organization_id, policy_id)
    if lock is not None:
        query = query.with_for_update(read=lock == "share")
    policy = session.scalar(query)
    if policy is None:
        raise NotFoundError("Policy not found.", code="policy_not_found")
    return policy


def create_policy(session: Session, organization_id: UUID, data: PolicyCreate) -> ApprovalPolicy:
    with transaction(session):
        approvers = _resolve_assignment(
            session, organization_id, data.required_approvals, data.approver_ids
        )
        policy = ApprovalPolicy(
            organization_id=organization_id,
            name=data.name,
            description=data.description,
            required_approvals=data.required_approvals,
        )
        policy.approver_links = _links(organization_id, approvers)
        session.add(policy)
        session.flush()
        policy_id = policy.id
    session.expire(policy)
    return get_policy(session, organization_id, policy_id)


def list_policies(
    session: Session,
    organization_id: UUID,
    *,
    limit: int,
    offset: int,
    is_active: bool | None = None,
) -> tuple[list[ApprovalPolicy], int]:
    conditions = [ApprovalPolicy.organization_id == organization_id]
    if is_active is not None:
        conditions.append(ApprovalPolicy.is_active == is_active)
    total = session.scalar(select(func.count()).select_from(ApprovalPolicy).where(*conditions)) or 0
    items = session.scalars(
        select(ApprovalPolicy)
        .where(*conditions)
        .options(selectinload(ApprovalPolicy.approver_links))
        .order_by(ApprovalPolicy.created_at.desc(), ApprovalPolicy.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(items), total


def update_policy(
    session: Session, organization_id: UUID, policy_id: UUID, data: PolicyUpdate
) -> ApprovalPolicy:
    """Apply changes and bump the version if anything changed.

    Existing requests are unaffected: they carry their own policy snapshot.
    """
    fields = data.model_fields_set
    with transaction(session):
        policy = get_policy(session, organization_id, policy_id, lock="update")
        changed = False

        for field in ("name", "description", "is_active"):
            if field in fields and getattr(policy, field) != getattr(data, field):
                setattr(policy, field, getattr(data, field))
                changed = True

        if fields & {"required_approvals", "approver_ids"}:
            current_ids = [link.approver_id for link in policy.approver_links]
            required = (
                data.required_approvals
                if data.required_approvals is not None
                else policy.required_approvals
            )
            approver_ids = data.approver_ids if data.approver_ids is not None else current_ids
            if required != policy.required_approvals or approver_ids != current_ids:
                approvers = _resolve_assignment(session, organization_id, required, approver_ids)
                policy.required_approvals = required
                # Remove old links before inserting new ones so reordering cannot trip the
                # (policy_id, position) unique constraint mid-flush.
                policy.approver_links.clear()
                session.flush()
                policy.approver_links.extend(_links(organization_id, approvers))
                changed = True

        if changed:
            policy.version += 1
    session.expire(policy)
    return get_policy(session, organization_id, policy_id)
