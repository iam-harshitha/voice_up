import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow
from app.domain.approval_rules import RequestStatus

_STATUSES = ", ".join(f"'{status.value}'" for status in RequestStatus)


class ApprovalRequest(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUSES})", name="status_valid"),
        CheckConstraint("length(btrim(action)) > 0", name="action_not_blank"),
        CheckConstraint("length(btrim(summary)) > 0", name="summary_not_blank"),
        CheckConstraint("jsonb_typeof(context) = 'object'", name="context_is_object"),
        CheckConstraint(
            "jsonb_typeof(policy_snapshot) = 'object'", name="policy_snapshot_is_object"
        ),
        # A decision timestamp exists exactly when the request is no longer pending.
        CheckConstraint(
            "(status = 'pending') = (decided_at IS NULL)", name="decided_at_matches_status"
        ),
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(
            ["policy_id", "organization_id"],
            ["approval_policies.id", "approval_policies.organization_id"],
            ondelete="RESTRICT",
        ),
        Index("ix_approval_requests_organization_id_created_at", "organization_id", "created_at"),
        Index("ix_approval_requests_status_expires_at", "status", "expires_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(index=True)
    policy_version: Mapped[int]
    action: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(String(2000))
    context: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    # Immutable copy of the rules that govern this request:
    # {"policy_id", "name", "version", "required_approvals", "approver_ids": [...]}
    policy_snapshot: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(String(16), default=RequestStatus.PENDING.value, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, server_default=func.now()
    )
    decided_at: Mapped[datetime | None]
    # Snapshotted deadline (created_at + policy deadline). Never caller-supplied.
    expires_at: Mapped[datetime | None]

    votes: Mapped[list["ApprovalVote"]] = relationship(  # noqa: F821
        order_by="[ApprovalVote.created_at, ApprovalVote.id]", viewonly=True
    )

    @property
    def required_approvals(self) -> int:
        return int(self.policy_snapshot["required_approvals"])

    @property
    def authorized_approver_ids(self) -> list[uuid.UUID]:
        """The primary approvers (seats) from the snapshot, in order."""
        return [uuid.UUID(value) for value in self.policy_snapshot["approver_ids"]]


class RequestApprover(Base):
    """Normalised, immutable copy of who may decide a request (primaries and backups).

    Votes reference this table, so the database itself rejects a vote from anyone who was
    not in the request's snapshot. A trigger (migration 0003) blocks UPDATE and DELETE.
    """

    __tablename__ = "request_approvers"
    __table_args__ = (
        CheckConstraint("role IN ('primary', 'backup')", name="role_valid"),
        CheckConstraint("role = 'primary' OR NOT can_veto", name="only_primaries_veto"),
        CheckConstraint("position >= 0", name="position_non_negative"),
        UniqueConstraint("request_id", "position"),
        ForeignKeyConstraint(
            ["request_id", "organization_id"],
            ["approval_requests.id", "approval_requests.organization_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["approver_id", "organization_id"],
            ["approvers.id", "approvers.organization_id"],
            ondelete="RESTRICT",
        ),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    approver_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    role: Mapped[str] = mapped_column(String(16))
    can_veto: Mapped[bool] = mapped_column(default=False)
    position: Mapped[int] = mapped_column(SmallInteger)
