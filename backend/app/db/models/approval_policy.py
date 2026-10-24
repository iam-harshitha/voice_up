import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    false,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow
from app.db.models.approver import Approver


class ApprovalPolicy(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "approval_policies"
    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint("required_approvals IN (1, 2)", name="required_approvals_supported"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("approval_mode IN ('unanimous', 'quorum')", name="approval_mode_valid"),
        CheckConstraint(
            "veto_mode IN ('any_rejection', 'designated', 'none')", name="veto_mode_valid"
        ),
        CheckConstraint(
            "approval_mode = 'quorum' OR veto_mode = 'any_rejection'",
            name="unanimous_uses_any_rejection",
        ),
        CheckConstraint(
            "decision_deadline_seconds IS NULL OR decision_deadline_seconds BETWEEN 60 AND 2592000",
            name="deadline_range",
        ),
        CheckConstraint(
            "(escalation_after_seconds IS NULL) = (max_escalations = 0)",
            name="escalation_configured_consistently",
        ),
        CheckConstraint(
            "escalation_after_seconds IS NULL OR escalation_after_seconds BETWEEN 60 AND 604800",
            name="escalation_after_range",
        ),
        CheckConstraint("max_escalations BETWEEN 0 AND 3", name="max_escalations_range"),
        CheckConstraint(
            "escalation_after_seconds IS NULL OR decision_deadline_seconds IS NULL "
            "OR escalation_after_seconds < decision_deadline_seconds",
            name="escalation_before_deadline",
        ),
        UniqueConstraint("id", "organization_id"),
        Index("ix_approval_policies_organization_id_is_active", "organization_id", "is_active"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(2000))
    required_approvals: Mapped[int] = mapped_column(SmallInteger)
    approval_mode: Mapped[str] = mapped_column(
        String(16), default="unanimous", server_default="unanimous"
    )
    veto_mode: Mapped[str] = mapped_column(
        String(16), default="any_rejection", server_default="any_rejection"
    )
    # Seconds after request creation at which a still-pending request expires (NULL = none).
    decision_deadline_seconds: Mapped[int | None]
    # Seconds between escalation rounds for undecided seats (NULL = no escalation).
    escalation_after_seconds: Mapped[int | None]
    max_escalations: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    version: Mapped[int] = mapped_column(default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, server_default=func.now()
    )

    approver_links: Mapped[list["PolicyApprover"]] = relationship(
        back_populates="policy",
        order_by="PolicyApprover.position",
        # delete-orphan lets a policy update replace its approver assignments.
        # Policies themselves are never deleted, only deactivated.
        cascade="all, delete-orphan",
    )

    @property
    def primary_links(self) -> list["PolicyApprover"]:
        return [link for link in self.approver_links if link.role == "primary"]

    @property
    def backup_links(self) -> list["PolicyApprover"]:
        return [link for link in self.approver_links if link.role == "backup"]

    @property
    def approvers(self) -> list[Approver]:
        """Primary approvers (the seats), in order."""
        return [link.approver for link in self.primary_links]

    @property
    def backup_approvers(self) -> list[Approver]:
        """Backup approvers, in escalation order."""
        return [link.approver for link in self.backup_links]

    @property
    def veto_approver_ids(self) -> list[uuid.UUID]:
        return [link.approver_id for link in self.primary_links if link.can_veto]


class PolicyApprover(Base):
    """Association between a policy and the approvers whose votes it requires."""

    __tablename__ = "policy_approvers"
    __table_args__ = (
        UniqueConstraint("policy_id", "position"),
        CheckConstraint("position >= 0", name="position_non_negative"),
        CheckConstraint("role IN ('primary', 'backup')", name="role_valid"),
        CheckConstraint("role = 'primary' OR NOT can_veto", name="only_primaries_veto"),
        ForeignKeyConstraint(
            ["policy_id", "organization_id"],
            ["approval_policies.id", "approval_policies.organization_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["approver_id", "organization_id"],
            ["approvers.id", "approvers.organization_id"],
            ondelete="RESTRICT",
        ),
    )

    # The composite primary key is the required (policy_id, approver_id) uniqueness.
    policy_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    approver_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    # Assignment order: primaries first, then backups in escalation order.
    position: Mapped[int] = mapped_column(SmallInteger)
    role: Mapped[str] = mapped_column(String(16), default="primary", server_default="primary")
    can_veto: Mapped[bool] = mapped_column(default=False, server_default=false())

    policy: Mapped[ApprovalPolicy] = relationship(back_populates="approver_links")
    # Read-only: approver_id is set explicitly, so the two composite foreign keys never
    # compete to write organization_id.
    approver: Mapped[Approver] = relationship(lazy="joined", viewonly=True)
