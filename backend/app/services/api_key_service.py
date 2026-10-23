"""Provisioning of organizations and API keys (used by the bootstrap CLI and tests)."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.core.security import generate_api_key, hash_api_key, parse_scopes
from app.db.base import utcnow
from app.db.models import ApiKey, Organization


@dataclass(frozen=True)
class IssuedApiKey:
    id: UUID
    organization_id: UUID
    name: str
    scopes: list[str]
    # Shown to the operator exactly once; only its hash is persisted.
    plaintext: str


def create_organization(session: Session, name: str) -> Organization:
    name = name.strip()
    if not name:
        raise ValueError("organization name must not be blank")
    organization = Organization(name=name)
    session.add(organization)
    session.flush()
    return organization


def issue_api_key(
    session: Session, organization_id: UUID, name: str, scopes: str | list[str]
) -> IssuedApiKey:
    name = name.strip()
    if not name:
        raise ValueError("key name must not be blank")
    if len(name) > 100:
        raise ValueError("key name must be at most 100 characters")
    normalized = parse_scopes(scopes)
    if session.get(Organization, organization_id) is None:
        raise NotFoundError("Organization not found.", code="organization_not_found")
    plaintext = generate_api_key()
    key = ApiKey(
        organization_id=organization_id,
        name=name,
        key_hash=hash_api_key(plaintext),
        scopes=normalized,
    )
    session.add(key)
    session.flush()
    return IssuedApiKey(key.id, organization_id, name, normalized, plaintext)


def revoke_api_key(session: Session, key_id: UUID) -> ApiKey:
    key = session.get(ApiKey, key_id, with_for_update=True)
    if key is None:
        raise NotFoundError("API key not found.", code="api_key_not_found")
    if key.revoked_at is None:
        key.revoked_at = utcnow()
    return key


def list_api_keys(session: Session, organization_id: UUID) -> list[ApiKey]:
    return list(
        session.scalars(
            select(ApiKey)
            .where(ApiKey.organization_id == organization_id)
            .order_by(ApiKey.created_at, ApiKey.id)
        )
    )
