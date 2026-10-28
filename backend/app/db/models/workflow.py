"""Chunk 3 workflow records: escalation steps and conditional approvals."""

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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow


class EscalationStep(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A backup assigned to the seat of a primary who had not decided in time.

    Immutable once written (trigger in migration 0003). The backup may then vote for that
    seat; the first valid vote for the seat (primary or backup) decides it.
    """

    __tablename__ = "escalation_steps"
    __table_args__ = (
        CheckConstraint("sequence BETWEEN 1 AND 3", name="sequence_range"),
        CheckConstraint("round >= 1", name="round_positive"),
        CheckConstraint("seat_approver_id <> backup_approver_id", name="backup_not_seat"),
        UniqueConstraint("request_id", "sequence"),
        # A backup serves at most one seat per request.
        UniqueConstraint("request_id", "backup_approver_id"),
        ForeignKeyConstraint(
            ["request_id", "organization_id"],
            ["approval_requests.id", "approval_requests.organization_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["request_id", "seat_approver_id"],
            ["request_approvers.request_id", "request_approvers.approver_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["request_id", "backup_approver_id"],
            ["request_approvers.request_id", "request_approvers.approver_id"],
            ondelete="RESTRICT",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    request_id: Mapped[uuid.UUID] = mapped_column(index=True)
    seat_approver_id: Mapped[uuid.UUID]
    backup_approver_id: Mapped[uuid.UUID]
    sequence: Mapped[int] = mapped_column(SmallInteger)
    round: Mapped[int] = mapped_column(SmallInteger)
    # Voice call placed to the backup, when voice is enabled and the backup has a phone.
    voice_call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("voice_calls.id", ondelete="RESTRICT")
    )
    # call_scheduled | voice_unavailable | no_phone_number | approver_inactive
    outcome: Mapped[str] = mapped_column(String(32))


class ConditionStatus:
    OPEN = "open"
    SATISFIED = "satisfied"  # resolved by an authorized actor; re-approval still required
    NOT_SATISFIED = "not_satisfied"  # resolved as not met; the seat is undecided again
    SUPERSEDED = "superseded"  # the approver later rejected outright
    LAPSED = "lapsed"  # the request reached a final state while it was open
    ALL = (OPEN, SATISFIED, NOT_SATISFIED, SUPERSEDED, LAPSED)
    RESOLUTIONS = (SATISFIED, NOT_SATISFIED)


class ApprovalCondition(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A conditional approval. It is never a vote and never grants authorization.

    Once its status leaves ``open`` the row is frozen (trigger in migration 0003).
    """

    __tablename__ = "approval_conditions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'satisfied', 'not_satisfied', 'superseded', 'lapsed')",
            name="status_valid",
        ),
        CheckConstraint("source IN ('voice', 'api')", name="source_valid"),
        CheckConstraint("length(btrim(description)) > 0", name="description_not_blank"),
        CheckConstraint("(status = 'open') = (closed_at IS NULL)", name="closed_at_matches"),
        CheckConstraint(
            "status NOT IN ('satisfied', 'not_satisfied') OR resolved_by_api_key_id IS NOT NULL",
            name="resolution_has_actor",
        ),
        ForeignKeyConstraint(
            ["request_id", "organization_id"],
            ["approval_requests.id", "approval_requests.organization_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["request_id", "approver_id"],
            ["request_approvers.request_id", "request_approvers.approver_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["request_id", "seat_approver_id"],
            ["request_approvers.request_id", "request_approvers.approver_id"],
            ondelete="RESTRICT",
        ),
        # At most one open condition per approver per request.
        Index(
            "uq_approval_conditions_open_per_approver",
            "request_id",
            "approver_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    request_id: Mapped[uuid.UUID] = mapped_column(index=True)
    approver_id: Mapped[uuid.UUID]
    seat_approver_id: Mapped[uuid.UUID]
    policy_version: Mapped[int]
    source: Mapped[str] = mapped_column(String(16))
    voice_call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("voice_calls.id", ondelete="RESTRICT")
    )
    # Normalised text of what the approver required. Stored as data; never interpreted.
    description: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), default=ConditionStatus.OPEN)
    closed_at: Mapped[datetime | None]
    resolved_by_api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="RESTRICT")
    )
    resolution_note: Mapped[str | None] = mapped_column(String(1000))
    evidence_reference: Mapped[str | None] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, server_default=func.now()
    )
