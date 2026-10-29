"""Approvers and approval policies, always scoped to the caller's organization."""

from collections.abc import Sequence
from dataclasses import dataclass, fields
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import BusinessRuleError, NotFoundError
from app.db.models import ApprovalPolicy, Approver, PolicyApprover
from app.db.session import transaction
from app.domain.approval_rules import (
    MAX_BACKUP_APPROVERS,
    MAX_PRIMARY_APPROVERS,
    ApprovalMode,
    InvalidPolicyError,
    PolicyRules,
    Seat,
    VetoMode,
    validate_required_approvals,
    validate_rules,
)
from app.schemas.policy import ApproverCreate, ApproverUpdate, PolicyCreate, PolicyUpdate
from app.services import audit_service
from app.services.audit_service import AuditEventType

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


@dataclass(frozen=True)
class PolicyConfig:
    """Every authorization-relevant policy setting, validated as one unit."""

    approval_mode: str
    required_approvals: int
    approver_ids: tuple[UUID, ...]
    veto_mode: str
    veto_approver_ids: tuple[UUID, ...]
    backup_approver_ids: tuple[UUID, ...]
    decision_deadline_seconds: int | None
    escalation_after_seconds: int | None
    max_escalations: int


RULE_FIELDS = frozenset(f.name for f in fields(PolicyConfig))


def _invalid(message: str) -> BusinessRuleError:
    return BusinessRuleError(message, code=POLICY_INVALID)


def _load_active(
    session: Session, organization_id: UUID, approver_ids: Sequence[UUID]
) -> dict[UUID, Approver]:
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
            raise _invalid(f"Approver {approver_id} was not found.")
        if not approver.is_active:
            raise _invalid(f"Approver {approver_id} is inactive.")
    return found


def _validate_config(
    session: Session, organization_id: UUID, config: PolicyConfig
) -> tuple[list[Approver], list[Approver]]:
    """Validate the whole configuration; return (primaries, backups) in order."""
    primaries, backups = list(config.approver_ids), list(config.backup_approver_ids)
    validate_required_approvals(config.required_approvals)
    if len(set(primaries)) != len(primaries):
        raise _invalid("Assigned approvers must be distinct.")
    if config.approval_mode == ApprovalMode.UNANIMOUS and len(primaries) != (
        config.required_approvals
    ):
        expected = (
            "exactly one authorized approver"
            if config.required_approvals == 1
            else "exactly two distinct authorized approvers"
        )
        raise _invalid(f"The policy must have {expected}.")
    if config.approval_mode == ApprovalMode.QUORUM and not (
        config.required_approvals <= len(primaries) <= MAX_PRIMARY_APPROVERS
    ):
        raise _invalid(
            f"A quorum policy needs between {config.required_approvals} and "
            f"{MAX_PRIMARY_APPROVERS} primary approvers."
        )
    if len(set(config.veto_approver_ids)) != len(config.veto_approver_ids) or not set(
        config.veto_approver_ids
    ) <= set(primaries):
        raise _invalid("Veto holders must be distinct primary approvers of this policy.")
    try:
        validate_rules(
            PolicyRules(
                ApprovalMode(config.approval_mode),
                config.required_approvals,
                VetoMode(config.veto_mode),
                tuple(Seat(a, a in config.veto_approver_ids) for a in primaries),
            )
        )
    except (InvalidPolicyError, ValueError) as exc:
        raise _invalid(f"Invalid policy rules: {exc}.") from exc

    if len(set(backups)) != len(backups):
        raise _invalid("Backup approvers must be distinct.")
    if set(backups) & set(primaries):
        raise _invalid("An approver cannot be both a primary and a backup.")
    if len(backups) > MAX_BACKUP_APPROVERS:
        raise _invalid(f"At most {MAX_BACKUP_APPROVERS} backup approvers.")
    if config.escalation_after_seconds is None:
        if config.max_escalations:
            raise _invalid("max_escalations requires escalation_after_seconds.")
    else:
        if not backups:
            raise _invalid("Escalation requires at least one backup approver.")
        if not 1 <= config.max_escalations <= len(backups):
            raise _invalid("max_escalations must be between 1 and the number of backups.")
        if (
            config.decision_deadline_seconds is not None
            and config.escalation_after_seconds >= config.decision_deadline_seconds
        ):
            raise _invalid("Escalation must start before the decision deadline.")

    found = _load_active(session, organization_id, primaries + backups)
    return [found[a] for a in primaries], [found[a] for a in backups]


def _links(
    organization_id: UUID, config: PolicyConfig, primaries: list[Approver], backups: list[Approver]
) -> list[PolicyApprover]:
    links = [
        PolicyApprover(
            approver_id=approver.id,
            organization_id=organization_id,
            position=index,
            role="primary",
            can_veto=approver.id in config.veto_approver_ids,
        )
        for index, approver in enumerate(primaries)
    ]
    links += [
        PolicyApprover(
            approver_id=approver.id,
            organization_id=organization_id,
            position=len(primaries) + index,
            role="backup",
            can_veto=False,
        )
        for index, approver in enumerate(backups)
    ]
    return links


def _config_of(policy: ApprovalPolicy) -> PolicyConfig:
    return PolicyConfig(
        approval_mode=policy.approval_mode,
        required_approvals=policy.required_approvals,
        approver_ids=tuple(link.approver_id for link in policy.primary_links),
        veto_mode=policy.veto_mode,
        veto_approver_ids=tuple(policy.veto_approver_ids),
        backup_approver_ids=tuple(link.approver_id for link in policy.backup_links),
        decision_deadline_seconds=policy.decision_deadline_seconds,
        escalation_after_seconds=policy.escalation_after_seconds,
        max_escalations=policy.max_escalations,
    )


def _config_data(config: PolicyConfig) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for name in RULE_FIELDS:
        value = getattr(config, name)
        data[name] = [str(v) for v in value] if isinstance(value, tuple) else value
    return data


def _apply_config(policy: ApprovalPolicy, config: PolicyConfig) -> None:
    policy.approval_mode = config.approval_mode
    policy.required_approvals = config.required_approvals
    policy.veto_mode = config.veto_mode
    policy.decision_deadline_seconds = config.decision_deadline_seconds
    policy.escalation_after_seconds = config.escalation_after_seconds
    policy.max_escalations = config.max_escalations


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


def _default_max_escalations(escalation_after: int | None, backups: Sequence[UUID]) -> int:
    return len(backups) if escalation_after is not None else 0


def _actor(actor_api_key_id: UUID | None) -> str | None:
    return str(actor_api_key_id) if actor_api_key_id else None


def create_policy(
    session: Session,
    organization_id: UUID,
    data: PolicyCreate,
    *,
    actor_api_key_id: UUID | None = None,
) -> ApprovalPolicy:
    config = PolicyConfig(
        approval_mode=data.approval_mode,
        required_approvals=data.required_approvals,
        approver_ids=tuple(data.approver_ids),
        veto_mode=data.veto_mode,
        veto_approver_ids=tuple(data.veto_approver_ids),
        backup_approver_ids=tuple(data.backup_approver_ids),
        decision_deadline_seconds=data.decision_deadline_seconds,
        escalation_after_seconds=data.escalation_after_seconds,
        max_escalations=(
            data.max_escalations
            if data.max_escalations is not None
            else _default_max_escalations(data.escalation_after_seconds, data.backup_approver_ids)
        ),
    )
    with transaction(session):
        primaries, backups = _validate_config(session, organization_id, config)
        policy = ApprovalPolicy(
            organization_id=organization_id,
            name=data.name,
            description=data.description,
            auto_dial=data.auto_dial,
        )
        _apply_config(policy, config)
        policy.approver_links = _links(organization_id, config, primaries, backups)
        session.add(policy)
        session.flush()
        audit_service.record_event(
            session,
            organization_id=organization_id,
            policy_id=policy.id,
            event_type=AuditEventType.POLICY_CREATED,
            data={
                "version": policy.version,
                "actor_api_key_id": _actor(actor_api_key_id),
                "auto_dial": policy.auto_dial,
                **_config_data(config),
            },
        )
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
    session: Session,
    organization_id: UUID,
    policy_id: UUID,
    data: PolicyUpdate,
    *,
    actor_api_key_id: UUID | None = None,
) -> ApprovalPolicy:
    """Apply changes and bump the version if anything changed.

    The merged configuration is validated as a whole. Existing requests are unaffected:
    they carry their own policy snapshot.
    """
    supplied = data.model_fields_set
    with transaction(session):
        policy = get_policy(session, organization_id, policy_id, lock="update")
        changed: list[str] = []

        for field in ("name", "description", "is_active", "auto_dial"):
            if field in supplied and getattr(policy, field) != getattr(data, field):
                setattr(policy, field, getattr(data, field))
                changed.append(field)

        current = _config_of(policy)
        if supplied & RULE_FIELDS:
            merged: dict[str, Any] = {}
            for name in RULE_FIELDS:
                if name in supplied:
                    value = getattr(data, name)
                    merged[name] = tuple(value) if isinstance(value, list) else value
                else:
                    merged[name] = getattr(current, name)
            if "max_escalations" not in supplied and (
                supplied & {"escalation_after_seconds", "backup_approver_ids"}
            ):
                merged["max_escalations"] = _default_max_escalations(
                    merged["escalation_after_seconds"], merged["backup_approver_ids"]
                )
            new = PolicyConfig(**merged)
            if new != current:
                primaries, backups = _validate_config(session, organization_id, new)
                _apply_config(policy, new)
                # Remove old links before inserting new ones so reordering cannot trip the
                # (policy_id, position) unique constraint mid-flush.
                policy.approver_links.clear()
                session.flush()
                policy.approver_links.extend(_links(organization_id, new, primaries, backups))
                changed += sorted(n for n in RULE_FIELDS if getattr(new, n) != getattr(current, n))
                current = new

        if changed:
            policy.version += 1
            audit_service.record_event(
                session,
                organization_id=organization_id,
                policy_id=policy.id,
                event_type=AuditEventType.POLICY_UPDATED,
                data={
                    "version": policy.version,
                    "changed_fields": changed,
                    "actor_api_key_id": _actor(actor_api_key_id),
                    "auto_dial": policy.auto_dial,
                    **_config_data(current),
                },
            )
    session.expire(policy)
    return get_policy(session, organization_id, policy_id)
