import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, true
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

E164_SQL_PATTERN = r"^\+[1-9][0-9]{1,14}$"


class Approver(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "approvers"
    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint("phone_number IS NOT NULL OR email IS NOT NULL", name="has_contact_method"),
        CheckConstraint(f"phone_number ~ '{E164_SQL_PATTERN}'", name="phone_number_e164"),
        # Target for composite foreign keys that pin children to the same organization.
        UniqueConstraint("id", "organization_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    phone_number: Mapped[str | None] = mapped_column(String(16))
    email: Mapped[str | None] = mapped_column(String(320))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
