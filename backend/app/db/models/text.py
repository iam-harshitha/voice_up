"""Text-message approvals (Telegram): verified approver links and per-approver prompts."""

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


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Channel:
    TELEGRAM = "telegram"
    ALL = (TELEGRAM,)


class LinkStatus:
    PENDING = "pending"  # pairing link issued, not used yet (may have expired)
    VERIFIED = "verified"  # the approver pressed Start with the one-time code
    REVOKED = "revoked"  # revoked, replaced or superseded; never valid again
    ALL = (PENDING, VERIFIED, REVOKED)


class ApproverChannelLink(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Binds one approver to one external account (a Telegram user and private chat).

    Pairing: an operator issues a one-time code (only its SHA-256 is stored, valid 10
    minutes); the approver opens t.me/<bot>?start=<code>; the bot sees the code with the
    sender's Telegram user ID, which Telegram itself supplies over our authenticated
    long poll. One verified link per approver per channel, and one approver per external
    user (globally: the bot is shared by every organization).
    """

    __tablename__ = "approver_channel_links"
    __table_args__ = (
        CheckConstraint(_in("channel", Channel.ALL), name="channel_valid"),
        CheckConstraint(_in("status", LinkStatus.ALL), name="status_valid"),
        CheckConstraint(
            "status <> 'pending' OR (pairing_code_hash IS NOT NULL "
            "AND pairing_expires_at IS NOT NULL)",
            name="pending_has_code",
        ),
        CheckConstraint(
            "status <> 'verified' OR (external_user_id IS NOT NULL "
            "AND external_chat_id IS NOT NULL AND verified_at IS NOT NULL)",
            name="verified_has_account",
        ),
        CheckConstraint(
            "(status = 'revoked') = (revoked_at IS NOT NULL)", name="revoked_at_matches"
        ),
        UniqueConstraint("id", "organization_id"),
        UniqueConstraint("pairing_code_hash"),
        ForeignKeyConstraint(
            ["approver_id", "organization_id"],
            ["approvers.id", "approvers.organization_id"],
            ondelete="RESTRICT",
        ),
        Index(
            "uq_approver_channel_links_verified_per_approver",
            "approver_id",
            "channel",
            unique=True,
            postgresql_where=text("status = 'verified'"),
        ),
        Index(
            "uq_approver_channel_links_verified_per_user",
            "channel",
            "external_user_id",
            unique=True,
            postgresql_where=text("status = 'verified'"),
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    approver_id: Mapped[uuid.UUID] = mapped_column(index=True)
    channel: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=LinkStatus.PENDING)
    # SHA-256 of the one-time pairing code. Kept after use so a second use is recognised.
    pairing_code_hash: Mapped[str | None] = mapped_column(String(64))
    pairing_expires_at: Mapped[datetime | None]
    # Telegram user ID and private chat ID (numeric strings). Never returned by the API
    # and never written to audit data.
    external_user_id: Mapped[str | None] = mapped_column(String(32))
    external_chat_id: Mapped[str | None] = mapped_column(String(32))
    created_by_api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="RESTRICT")
    )
    verified_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class PromptStatus:
    QUEUED = "queued"  # waiting for the text worker
    SENDING = "sending"  # the worker is sending it (leased)
    SENT = "sent"  # delivered with Approve / Reject buttons
    CONFIRMING = "confirming"  # Approve tapped; waiting for Confirm / Cancel
    ANSWERED = "answered"  # a valid tap consumed it (see outcome)
    CLOSED = "closed"  # expired, or the request was decided another way
    FAILED = "failed"  # could not be delivered
    OPEN = (QUEUED, SENDING, SENT, CONFIRMING)
    ALL = (QUEUED, SENDING, SENT, CONFIRMING, ANSWERED, CLOSED, FAILED)


class PromptReason:
    AUTO_DIAL = "auto_dial"
    ESCALATION = "escalation"
    REAPPROVAL = "reapproval"
    ALL = (AUTO_DIAL, ESCALATION, REAPPROVAL)


class TextPrompt(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One approval message to one snapshotted approver of one request.

    Button tokens are single-use: only the SHA-256 of each stage's random nonce is
    stored (``token_hash`` for Approve / Reject, ``confirm_token_hash`` for Confirm /
    Cancel), and every tap is checked against the prompt's status under a row lock.
    """

    __tablename__ = "text_prompts"
    __table_args__ = (
        CheckConstraint(_in("status", PromptStatus.ALL), name="status_valid"),
        CheckConstraint(_in("reason", PromptReason.ALL), name="reason_valid"),
        CheckConstraint(_in("provider", ("telegram", "mock")), name="provider_valid"),
        CheckConstraint(
            "decision IS NULL OR decision IN ('approve', 'reject')", name="decision_valid"
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(
            "(status = 'sending') = (lease_expires_at IS NOT NULL)", name="lease_matches_status"
        ),
        CheckConstraint(
            "status NOT IN ('sent', 'confirming') OR (token_hash IS NOT NULL "
            "AND provider_message_id IS NOT NULL AND chat_id IS NOT NULL)",
            name="sent_has_message",
        ),
        CheckConstraint(
            "status <> 'confirming' OR confirm_token_hash IS NOT NULL",
            name="confirming_has_token",
        ),
        CheckConstraint(
            "(status = 'answered') = (answered_at IS NOT NULL)", name="answered_at_matches"
        ),
        UniqueConstraint("token_hash"),
        UniqueConstraint("confirm_token_hash"),
        ForeignKeyConstraint(
            ["request_id", "organization_id"],
            ["approval_requests.id", "approval_requests.organization_id"],
            ondelete="RESTRICT",
        ),
        # Only approvers in the request's snapshot can be prompted.
        ForeignKeyConstraint(
            ["request_id", "approver_id"],
            ["request_approvers.request_id", "request_approvers.approver_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["link_id", "organization_id"],
            ["approver_channel_links.id", "approver_channel_links.organization_id"],
            ondelete="RESTRICT",
        ),
        Index("ix_text_prompts_status_run_after", "status", "run_after"),
        # One open prompt per approver per request.
        Index(
            "uq_text_prompts_open_per_approver",
            "request_id",
            "approver_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'sending', 'sent', 'confirming')"),
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    request_id: Mapped[uuid.UUID] = mapped_column(index=True)
    approver_id: Mapped[uuid.UUID]
    link_id: Mapped[uuid.UUID] = mapped_column(index=True)
    provider: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=PromptStatus.QUEUED)
    token_hash: Mapped[str | None] = mapped_column(String(64))
    confirm_token_hash: Mapped[str | None] = mapped_column(String(64))
    decision: Mapped[str | None] = mapped_column(String(16))
    # Safe result code: vote_recorded, vote_already_recorded, an approval-service error
    # code, expired, request_closed, link_revoked, a send error kind, ...
    outcome: Mapped[str | None] = mapped_column(String(48))
    vote_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("approval_votes.id", ondelete="RESTRICT")
    )
    # Where the message lives (a copy: the link may later be revoked or replaced).
    chat_id: Mapped[str | None] = mapped_column(String(32))
    provider_message_id: Mapped[str | None] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    run_after: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    lease_expires_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(String(64))
    # The earlier of now + TEXT_PROMPT_TTL_SECONDS and the request's deadline.
    expires_at: Mapped[datetime]
    sent_at: Mapped[datetime | None]
    answered_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, server_default=func.now()
    )
