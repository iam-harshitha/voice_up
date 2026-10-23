import uuid
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKeyConstraint, Identity, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class AuditEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only record of what happened to a request.

    Application code only ever inserts rows (see services/audit_service.py). The initial
    migration also installs a trigger that rejects UPDATE and DELETE on this table.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("length(btrim(event_type)) > 0", name="event_type_not_blank"),
        CheckConstraint("jsonb_typeof(event_data) = 'object'", name="event_data_is_object"),
        ForeignKeyConstraint(
            ["request_id", "organization_id"],
            ["approval_requests.id", "approval_requests.organization_id"],
            ondelete="RESTRICT",
        ),
        Index("ix_audit_events_request_id_created_at", "request_id", "created_at"),
        Index("ix_audit_events_request_id_sequence", "request_id", "sequence"),
    )

    # Strictly increasing insertion order. Events written in one transaction can share a
    # created_at value, so timelines order by sequence rather than by timestamp.
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)

    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    request_id: Mapped[uuid.UUID]
    event_type: Mapped[str] = mapped_column(String(64))
    event_data: Mapped[dict[str, Any]]
