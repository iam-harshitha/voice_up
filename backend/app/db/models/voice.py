"""Voice-approval persistence: intended calls, the durable job queue, provider events."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Float,
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
from app.voice.base import CallStatus


def _in(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(str(v)) for v in values)})"


class DecisionStatus:
    AWAITING = "awaiting"  # no final call result yet
    ACCEPTED = "accepted"  # a vote was recorded through the approval service
    NO_DECISION = "no_decision"  # call ended without a valid explicit decision
    REFUSED = "refused"  # valid decision, but the approval service refused the vote

    ALL = (AWAITING, ACCEPTED, NO_DECISION, REFUSED)


class VoiceCall(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One intended call to one snapshotted approver of one request.

    request_id / approver_id / organization_id / provider are immutable, and
    provider_call_id can be set once; a trigger in migration 0002 enforces this.
    """

    __tablename__ = "voice_calls"
    __table_args__ = (
        CheckConstraint(_in("status", CallStatus), name="status_valid"),
        CheckConstraint(_in("decision_status", DecisionStatus.ALL), name="decision_status_valid"),
        CheckConstraint("generation > 0", name="generation_positive"),
        CheckConstraint("initiation_attempts >= 0", name="initiation_attempts_non_negative"),
        UniqueConstraint("request_id", "approver_id", "generation"),
        UniqueConstraint("provider", "provider_call_id"),
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
        Index("ix_voice_calls_status_last_event_at", "status", "last_event_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    request_id: Mapped[uuid.UUID] = mapped_column(index=True)
    approver_id: Mapped[uuid.UUID] = mapped_column(index=True)
    provider: Mapped[str] = mapped_column(String(16))
    generation: Mapped[int] = mapped_column(SmallInteger, default=1)
    status: Mapped[str] = mapped_column(String(16), default=CallStatus.QUEUED.value)
    provider_call_id: Mapped[str | None] = mapped_column(String(64))
    provider_status: Mapped[str | None] = mapped_column(String(32))
    # Number dialled, kept to verify the provider's execution belongs to this call.
    # Never returned by the API or written to audit data.
    recipient_phone_number: Mapped[str | None] = mapped_column(String(16))
    initiation_attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    decision_status: Mapped[str] = mapped_column(
        String(16), default=DecisionStatus.AWAITING, server_default=DecisionStatus.AWAITING
    )
    decision_candidate: Mapped[str | None] = mapped_column(String(32))
    decision_confidence: Mapped[float | None] = mapped_column(Float)
    # Safe, normalised result code (e.g. vote_recorded, uncertain, voicemail, no-answer).
    outcome: Mapped[str | None] = mapped_column(String(48))
    vote_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("approval_votes.id", ondelete="RESTRICT")
    )
    last_error: Mapped[str | None] = mapped_column(String(64))
    initiated_at: Mapped[datetime | None]
    answered_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]
    last_event_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class JobType:
    INITIATE_CALL = "initiate_call"
    PROCESS_EVENT = "process_event"
    RECONCILE_CALL = "reconcile_call"
    ALL = (INITIATE_CALL, PROCESS_EVENT, RECONCILE_CALL)


class JobStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ALL = (PENDING, PROCESSING, SUCCEEDED, FAILED)


class VoiceJob(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """PostgreSQL-backed work queue, claimed with FOR UPDATE SKIP LOCKED and leased."""

    __tablename__ = "voice_jobs"
    __table_args__ = (
        CheckConstraint(_in("job_type", JobType.ALL), name="job_type_valid"),
        CheckConstraint(_in("status", JobStatus.ALL), name="status_valid"),
        CheckConstraint("attempts >= 0 AND max_attempts > 0", name="attempts_valid"),
        CheckConstraint(
            "(job_type = 'process_event') = (event_id IS NOT NULL)", name="event_job_has_event"
        ),
        CheckConstraint(
            "job_type = 'process_event' OR voice_call_id IS NOT NULL", name="call_job_has_call"
        ),
        CheckConstraint(
            "(status = 'processing') = (lease_expires_at IS NOT NULL)", name="lease_matches_status"
        ),
        Index("ix_voice_jobs_claim", "status", "run_after"),
        # One initiation job per call: initiation is never re-queued automatically.
        Index(
            "uq_voice_jobs_initiate_per_call",
            "voice_call_id",
            unique=True,
            postgresql_where=text("job_type = 'initiate_call'"),
        ),
        # At most one active reconciliation per call.
        Index(
            "uq_voice_jobs_active_reconcile_per_call",
            "voice_call_id",
            unique=True,
            postgresql_where=text(
                "job_type = 'reconcile_call' AND status IN ('pending', 'processing')"
            ),
        ),
    )

    job_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.PENDING)
    voice_call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("voice_calls.id", ondelete="RESTRICT"), index=True
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("voice_provider_events.id", ondelete="RESTRICT"), unique=True
    )
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    max_attempts: Mapped[int]
    run_after: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    lease_expires_at: Mapped[datetime | None]
    locked_by: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class EventStatus:
    RECEIVED = "received"  # stored; a process_event job will handle it
    PROCESSED = "processed"  # effects committed
    UNMATCHED = "unmatched"  # no call known yet for this provider call ID
    IGNORED = "ignored"  # authenticated but not ours / not applicable
    FAILED = "failed"  # retries exhausted; reconciliation polling still covers the call
    ALL = (RECEIVED, PROCESSED, UNMATCHED, IGNORED, FAILED)


class VoiceProviderEvent(UUIDPrimaryKeyMixin, Base):
    """Durable inbox of authenticated provider webhooks (minimal fields, no raw body).

    Bolna sends no event ID, so deliveries are deduplicated on a hash of
    (provider call ID, provider status, provider updated_at).
    """

    __tablename__ = "voice_provider_events"
    __table_args__ = (
        CheckConstraint(_in("status", EventStatus.ALL), name="status_valid"),
        UniqueConstraint("provider", "dedup_key"),
    )

    provider: Mapped[str] = mapped_column(String(16))
    dedup_key: Mapped[str] = mapped_column(String(64))
    provider_call_id: Mapped[str] = mapped_column(String(64), index=True)
    provider_status: Mapped[str] = mapped_column(String(32))
    provider_updated_at: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=EventStatus.RECEIVED)
    result: Mapped[str | None] = mapped_column(String(48))
    voice_call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("voice_calls.id", ondelete="RESTRICT"), index=True
    )
    received_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    processed_at: Mapped[datetime | None]
