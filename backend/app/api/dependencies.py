"""Request-scoped dependencies: database session, authentication, tenant context, scopes.

The three security layers are deliberately separate:
  authenticate            -> who is calling (a valid, unrevoked API key)       401
  get_organization_context -> which tenant the call acts for (from the key only)
  require_scope(...)      -> whether the key may perform this operation        403
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Query, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.security import MAX_API_KEY_LENGTH, Scope, hash_api_key
from app.db.models import ApiKey
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_OFFSET, MAX_PAGE_SIZE

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_db(request: Request) -> Iterator[Session]:
    session: Session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_db)]


@dataclass(frozen=True)
class Principal:
    api_key_id: UUID
    organization_id: UUID
    scopes: frozenset[str]


@dataclass(frozen=True)
class OrganizationContext:
    organization_id: UUID
    principal: Principal


def authenticate(
    session: DbSession, api_key: Annotated[str | None, Security(api_key_header)]
) -> Principal:
    if not api_key:
        raise AuthenticationError("An X-API-Key header is required.", code="api_key_missing")
    if len(api_key) > MAX_API_KEY_LENGTH:
        raise AuthenticationError("The API key is invalid.", code="api_key_invalid")
    key = session.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(api_key)))
    # Unknown and revoked keys get the same response.
    if key is None or key.revoked_at is not None:
        raise AuthenticationError("The API key is invalid.", code="api_key_invalid")
    principal = Principal(key.id, key.organization_id, frozenset(key.scopes))
    # End the read-only transaction so later service transactions start fresh.
    session.rollback()
    return principal


def get_organization_context(
    principal: Annotated[Principal, Depends(authenticate)],
) -> OrganizationContext:
    # The tenant is always derived from the credential, never from the request payload.
    return OrganizationContext(principal.organization_id, principal)


def require_scope(scope: Scope) -> Callable[..., OrganizationContext]:
    def dependency(
        context: Annotated[OrganizationContext, Depends(get_organization_context)],
    ) -> OrganizationContext:
        if scope.value not in context.principal.scopes:
            raise PermissionDeniedError(f"This API key lacks the '{scope.value}' scope.")
        return context

    return dependency


@dataclass(frozen=True)
class Pagination:
    limit: int
    offset: int


def pagination(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> Pagination:
    return Pagination(limit, offset)


PageParams = Annotated[Pagination, Depends(pagination)]
