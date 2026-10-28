"""Dashboard sign-in: users, password verification, lockout and server-side sessions.

Security properties:
- Passwords are stored as scrypt hashes (app/core/passwords.py); never logged.
- Session tokens are 256-bit random values sent only in an HttpOnly cookie. Only their
  SHA-256 hash is stored, so a database read does not yield usable sessions.
- The CSRF token is derived from the session token (it cannot be computed without the
  cookie) and must accompany every state-changing request made with the cookie.
- Unknown emails, wrong passwords, inactive users and locked accounts all get the same
  response, and unknown emails still pay for one hash verification.
- After ``dashboard_max_failed_logins`` consecutive failures the account is locked for
  ``dashboard_lockout_seconds`` (durable, so it holds across processes and restarts).
- Each user is backed by an internal API key (plaintext discarded). Its scopes and
  organization are the user's; revoking it ends every session immediately.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.core.passwords import DUMMY_PASSWORD_HASH, hash_password, verify_password
from app.core.security import Scope, parse_scopes
from app.db.base import utcnow
from app.db.models import ApiKey, DashboardSession, DashboardUser, Organization
from app.db.session import transaction
from app.services import api_key_service

SESSION_COOKIE = "al_session"
CSRF_HEADER = "X-CSRF-Token"
_SESSION_TOKEN_BYTES = 32
MAX_SESSION_TOKEN_LENGTH = 128

# Dashboard users never get votes:write: the dashboard must not let an administrator
# cast votes on behalf of approvers. Votes come from verified voice calls or a trusted
# integration key.
FORBIDDEN_DASHBOARD_SCOPES = frozenset({Scope.VOTES_WRITE.value})

LOGIN_FAILED_MESSAGE = (
    "Sign-in failed. Check your email and password, or try again later if the account "
    "was locked after repeated failures."
)


@dataclass(frozen=True)
class SessionIdentity:
    session_id: UUID
    user_id: UUID
    api_key_id: UUID
    organization_id: UUID
    organization_name: str
    email: str
    display_name: str
    scopes: frozenset[str]
    expires_at: datetime


@dataclass(frozen=True)
class NewSession:
    token: str
    identity: SessionIdentity


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def csrf_token_for(session_token: str) -> str:
    return hashlib.sha256(b"approval-line-csrf:" + session_token.encode("utf-8")).hexdigest()


def csrf_matches(session_token: str, presented: str | None) -> bool:
    if not presented or len(presented) > 128:
        return False
    return hmac.compare_digest(csrf_token_for(session_token), presented)


def normalize_email(email: str) -> str:
    try:
        return validate_email(email.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise ValueError("email address is invalid") from exc


def create_user(
    session: Session,
    organization_id: UUID,
    *,
    email: str,
    display_name: str,
    password: str,
    scopes: str | list[str],
) -> DashboardUser:
    """Create a dashboard user and its internal API key (inside the caller's transaction)."""
    normalized_scopes = parse_scopes(scopes)
    forbidden = sorted(set(normalized_scopes) & FORBIDDEN_DASHBOARD_SCOPES)
    if forbidden:
        raise ValueError(f"dashboard users cannot hold: {', '.join(forbidden)}")
    display_name = display_name.strip()
    if not display_name or len(display_name) > 200:
        raise ValueError("display name must be 1-200 characters")
    email = normalize_email(email)
    if session.scalar(select(DashboardUser.id).where(DashboardUser.email == email)):
        raise ValueError("a dashboard user with this email already exists")
    password_hash = hash_password(password)
    # The plaintext of this key is discarded: it exists only to carry scopes and
    # attribution. It cannot be used as an X-API-Key because nobody knows it.
    key = api_key_service.issue_api_key(
        session, organization_id, f"dashboard:{email}"[:100], normalized_scopes
    )
    user = DashboardUser(
        organization_id=organization_id,
        api_key_id=key.id,
        email=email,
        display_name=display_name,
        password_hash=password_hash,
    )
    session.add(user)
    session.flush()
    return user


def login(
    session: Session, settings: Settings, email: str, password: str, *, now: datetime | None = None
) -> NewSession:
    now = now or utcnow()
    try:
        normalized = normalize_email(email)
    except ValueError:
        normalized = None
    with transaction(session):
        user = (
            session.scalar(
                select(DashboardUser).where(DashboardUser.email == normalized).with_for_update()
            )
            if normalized
            else None
        )
        if user is None:
            verify_password(password, DUMMY_PASSWORD_HASH)
            failed = True
        else:
            key = session.get(ApiKey, user.api_key_id)
            locked = user.locked_until is not None and user.locked_until > now
            usable = user.is_active and key is not None and key.revoked_at is None
            password_ok = verify_password(password, user.password_hash)
            failed = locked or not usable or not password_ok
            if not password_ok and not locked:
                user.failed_login_count += 1
                if user.failed_login_count >= settings.dashboard_max_failed_logins:
                    user.locked_until = now + timedelta(seconds=settings.dashboard_lockout_seconds)
                    user.failed_login_count = 0
            if not failed:
                user.failed_login_count = 0
                user.locked_until = None
                user.last_login_at = now
                token = secrets.token_urlsafe(_SESSION_TOKEN_BYTES)
                record = DashboardSession(
                    user_id=user.id,
                    organization_id=user.organization_id,
                    token_hash=hash_token(token),
                    created_at=now,
                    expires_at=now + timedelta(seconds=settings.dashboard_session_ttl_seconds),
                )
                session.add(record)
                session.flush()
                session_id = record.id
    # Failed-attempt counters are committed above, before the error is raised.
    if failed:
        raise AuthenticationError(LOGIN_FAILED_MESSAGE, code="login_failed")
    identity = resolve_session(session, token, now=now)
    if identity is None or identity.session_id != session_id:  # pragma: no cover - just made
        raise AuthenticationError(LOGIN_FAILED_MESSAGE, code="login_failed")
    return NewSession(token, identity)


def resolve_session(
    session: Session, token: str, *, now: datetime | None = None
) -> SessionIdentity | None:
    """The identity behind a session cookie, or None if it is unknown or no longer valid."""
    if not token or len(token) > MAX_SESSION_TOKEN_LENGTH:
        return None
    now = now or utcnow()
    row = session.execute(
        select(DashboardSession, DashboardUser, ApiKey, Organization)
        .join(DashboardUser, DashboardUser.id == DashboardSession.user_id)
        .join(ApiKey, ApiKey.id == DashboardUser.api_key_id)
        .join(Organization, Organization.id == DashboardUser.organization_id)
        .where(DashboardSession.token_hash == hash_token(token))
    ).first()
    if row is None:
        return None
    record, user, key, organization = row
    if (
        record.revoked_at is not None
        or record.expires_at <= now
        or not user.is_active
        or key.revoked_at is not None
        or key.organization_id != user.organization_id
        or record.organization_id != user.organization_id
    ):
        return None
    return SessionIdentity(
        session_id=record.id,
        user_id=user.id,
        api_key_id=key.id,
        organization_id=user.organization_id,
        organization_name=organization.name,
        email=user.email,
        display_name=user.display_name,
        scopes=frozenset(key.scopes),
        expires_at=record.expires_at,
    )


def logout(session: Session, token: str) -> None:
    """Revoke the session behind ``token`` (no-op for unknown tokens)."""
    if not token or len(token) > MAX_SESSION_TOKEN_LENGTH:
        return
    with transaction(session):
        record = session.scalar(
            select(DashboardSession)
            .where(DashboardSession.token_hash == hash_token(token))
            .with_for_update()
        )
        if record is not None and record.revoked_at is None:
            record.revoked_at = utcnow()


def revoke_user_sessions(session: Session, user_id: UUID) -> int:
    """Revoke every open session of a user (inside the caller's transaction)."""
    now = utcnow()
    count = 0
    for record in session.scalars(
        select(DashboardSession).where(
            DashboardSession.user_id == user_id, DashboardSession.revoked_at.is_(None)
        )
    ):
        record.revoked_at = now
        count += 1
    return count


def disable_user(session: Session, email: str) -> DashboardUser:
    """Deactivate a user, revoke its sessions and its backing key (caller's transaction)."""
    user = session.scalar(
        select(DashboardUser).where(DashboardUser.email == normalize_email(email)).with_for_update()
    )
    if user is None:
        raise ValueError("no dashboard user with this email")
    user.is_active = False
    revoke_user_sessions(session, user.id)
    api_key_service.revoke_api_key(session, user.api_key_id)
    return user
