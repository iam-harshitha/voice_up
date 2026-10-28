"""Idempotent demo setup for an EXISTING organization (Chunk 5).

Creates, through the normal policy service (validation, audit events), only what the
documented demo needs:

- three fictional approvers (reserved 555-01xx numbers): "Demo Alice", "Demo Bob",
  "Demo Carol (backup)";
- "Demo: production deploys": unanimous, Alice + Bob, 1-hour deadline;
- "Demo: short deadline": unanimous, Alice + Bob, 2-minute deadline (expiry scenario).

It never creates requests, votes, conditions or calls: those come from the demo agent
and the operator's simulated (or real) phone answers, through the same workflow as
production. Records are matched by name inside the given organization. A matching
record with the same configuration is reused; one with a different configuration is
left untouched and reported as a conflict (never overwritten). Nothing is deleted.
"""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.db.models import ApprovalPolicy, Approver, Organization
from app.schemas.policy import ApproverCreate, PolicyCreate
from app.services import policy_service

APPROVERS: tuple[tuple[str, str], ...] = (
    ("Demo Alice", "+14155550101"),
    ("Demo Bob", "+14155550102"),
    ("Demo Carol (backup)", "+14155550103"),
)
PRODUCTION_POLICY = "Demo: production deploys"
SHORT_DEADLINE_POLICY = "Demo: short deadline"
POLICIES: tuple[tuple[str, int], ...] = ((PRODUCTION_POLICY, 3600), (SHORT_DEADLINE_POLICY, 120))


class DemoSeedError(Exception):
    """The organization's existing data conflicts with the demo setup."""


@dataclass
class SeedResult:
    organization_name: str
    approvers: dict[str, UUID] = field(default_factory=dict)
    policies: dict[str, UUID] = field(default_factory=dict)
    created: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)


def _approver(session: Session, organization_id: UUID, name: str) -> Approver | None:
    found = session.scalar(
        select(Approver).where(Approver.organization_id == organization_id, Approver.name == name)
    )
    session.rollback()
    return found


def _policy(session: Session, organization_id: UUID, name: str) -> ApprovalPolicy | None:
    found = session.scalar(
        select(ApprovalPolicy).where(
            ApprovalPolicy.organization_id == organization_id, ApprovalPolicy.name == name
        )
    )
    if found is not None:
        found = policy_service.get_policy(session, organization_id, found.id)
    session.rollback()
    return found


def seed(session: Session, organization_id: UUID) -> SeedResult:
    organization = session.get(Organization, organization_id)
    session.rollback()
    if organization is None:
        raise NotFoundError("Organization not found.", code="organization_not_found")
    result = SeedResult(organization.name)

    for name, phone in APPROVERS:
        existing = _approver(session, organization_id, name)
        if existing is None:
            created = policy_service.create_approver(
                session, organization_id, ApproverCreate(name=name, phone_number=phone)
            )
            result.approvers[name] = created.id
            result.created.append(f"approver {name}")
            continue
        if not existing.is_active or existing.phone_number != phone:
            raise DemoSeedError(
                f"An approver named {name!r} already exists with different settings "
                "(inactive or another phone number). Rename or update it; it was not changed."
            )
        result.approvers[name] = existing.id
        result.reused.append(f"approver {name}")

    primaries = [result.approvers["Demo Alice"], result.approvers["Demo Bob"]]
    for name, deadline in POLICIES:
        existing = _policy(session, organization_id, name)
        if existing is None:
            created = policy_service.create_policy(
                session,
                organization_id,
                PolicyCreate(
                    name=name,
                    description="Created by app.cli demo-seed for the Approval Line demo.",
                    approval_mode="unanimous",
                    required_approvals=2,
                    approver_ids=primaries,
                    decision_deadline_seconds=deadline,
                ),
            )
            result.policies[name] = created.id
            result.created.append(f"policy {name}")
            continue
        same = (
            existing.is_active
            and existing.approval_mode == "unanimous"
            and existing.required_approvals == 2
            and [a.id for a in existing.approvers] == primaries
            and existing.decision_deadline_seconds == deadline
            and not existing.backup_approvers
        )
        if not same:
            raise DemoSeedError(
                f"A policy named {name!r} already exists with a different configuration. "
                "It was not changed; rename or deactivate it, then run demo-seed again."
            )
        result.policies[name] = existing.id
        result.reused.append(f"policy {name}")
    return result
