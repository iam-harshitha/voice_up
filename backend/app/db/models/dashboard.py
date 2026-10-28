"""Dashboard (browser) users and their server-side sessions.

A dashboard user signs in with an email and password and receives an opaque session
cookie. Each user is backed by an internal API key whose plaintext is never issued: the
key carries the user's scopes and organization, so every existing scope check, tenant
filter and actor attribution (``resolved_by_api_key_id``, policy ``actor_api_key_id``)
applies unchanged to dashboard actions, and revoking the key ends all of its sessions.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, func, true
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin, utcnow


class DashboardUser(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "dashboard_users"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint("length(btrim(email)) > 3", name="email_not_blank"),
        CheckConstraint("length(btrim(display_name)) > 0", name="display_name_not_blank"),
        CheckConstraint("failed_login_count >= 0", name="failed_login_count_non_negative"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    # The internal key that carries this user's scopes. Never issued as plaintext.
    api_key_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("api_keys.id", ondelete="RESTRICT"), unique=True
    )
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    # scrypt hash in a self-describing format (see app/core/passwords.py).
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    failed_login_count: Mapped[int] = mapped_column(default=0, server_default="0")
    locked_until: Mapped[datetime | None]
    last_login_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class DashboardSession(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "dashboard_sessions"
    __table_args__ = (CheckConstraint("expires_at > created_at", name="expires_after_creation"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dashboard_users.id", ondelete="RESTRICT"), index=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    # SHA-256 hex digest of the cookie value. The token itself is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
