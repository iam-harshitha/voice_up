"""Request-scoped dependencies: database session, authentication, tenant context, scopes.

The three security layers are deliberately separate:
  authenticate            -> who is calling (a valid, unrevoked API key, or a  401
                             valid dashboard session cookie + CSRF token)
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
from app.services import dashboard_auth_service

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
    # For a dashboard session this is the user's internal backing key, so attribution
    # (resolved_by_api_key_id, actor_api_key_id) works the same for both kinds of caller.
    api_key_id: UUID
    organization_id: UUID
    scopes: frozenset[str]
    dashboard_user_id: UUID | None = None


@dataclass(frozen=True)
class OrganizationContext:
    organization_id: UUID
    principal: Principal


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _session_principal(request: Request, session: Session, token: str) -> Principal:
    identity = dashboard_auth_service.resolve_session(session, token)
    session.rollback()
    if identity is None:
        raise AuthenticationError(
            "The dashboard session is invalid or has expired.", code="session_invalid"
        )
    # Cookies are sent automatically by browsers, so every state-changing request made
    # with one must also prove it came from our page (double-submit, derived token).
    if request.method not in _SAFE_METHODS and not dashboard_auth_service.csrf_matches(
        token, request.headers.get(dashboard_auth_service.CSRF_HEADER)
    ):
        raise PermissionDeniedError("A valid CSRF token is required.", code="csrf_failed")
    return Principal(
        identity.api_key_id, identity.organization_id, identity.scopes, identity.user_id
    )


def authenticate(
    request: Request,
    session: DbSession,
    api_key: Annotated[str | None, Security(api_key_header)],
) -> Principal:
    if not api_key:
        # Agents and integrations use X-API-Key; the dashboard uses its session cookie.
        token = request.cookies.get(dashboard_auth_service.SESSION_COOKIE)
        if token:
            return _session_principal(request, session, token)
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
