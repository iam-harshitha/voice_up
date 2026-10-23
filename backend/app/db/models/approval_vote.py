import uuid

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.domain.approval_rules import VoteChoice

_CHOICES = ", ".join(f"'{choice.value}'" for choice in VoteChoice)


class ApprovalVote(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "approval_votes"
    __table_args__ = (
        CheckConstraint(f"vote IN ({_CHOICES})", name="vote_valid"),
        # One final vote per approver per request; the second line of defence behind the
        # row lock taken by the vote service.
        UniqueConstraint("request_id", "approver_id"),
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

    # organization_id is denormalised from the request so the composite foreign keys
    # make a cross-organization vote impossible at the database level.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    request_id: Mapped[uuid.UUID]
    approver_id: Mapped[uuid.UUID] = mapped_column(index=True)
    vote: Mapped[str] = mapped_column(String(16))
