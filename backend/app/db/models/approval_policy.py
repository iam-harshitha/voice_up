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
        UniqueConstraint("id", "organization_id"),
        Index("ix_approval_policies_organization_id_is_active", "organization_id", "is_active"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(2000))
    required_approvals: Mapped[int] = mapped_column(SmallInteger)
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
    def approvers(self) -> list[Approver]:
        return [link.approver for link in self.approver_links]


class PolicyApprover(Base):
    """Association between a policy and the approvers whose votes it requires."""

    __tablename__ = "policy_approvers"
    __table_args__ = (
        UniqueConstraint("policy_id", "position"),
        CheckConstraint("position >= 0", name="position_non_negative"),
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
    # Preserves the order approvers were assigned in (e.g. who is contacted first later).
    position: Mapped[int] = mapped_column(SmallInteger)

    policy: Mapped[ApprovalPolicy] = relationship(back_populates="approver_links")
    # Read-only: approver_id is set explicitly, so the two composite foreign keys never
    # compete to write organization_id.
    approver: Mapped[Approver] = relationship(lazy="joined", viewonly=True)
