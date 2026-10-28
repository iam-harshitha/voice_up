"""Dashboard sign-in. Agents and integrations keep using X-API-Key and never need this.

The session token travels only in an HttpOnly cookie scoped to the API path; the
response body carries the derived CSRF token, which the dashboard sends back in the
X-CSRF-Token header on every state-changing request.
"""

from fastapi import APIRouter, Request, Response, status

from app.api.dependencies import DbSession
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.schemas.auth import (
    EnvironmentRead,
    LoginRequest,
    OrganizationRef,
    SessionRead,
    SessionUserRead,
)
from app.services import dashboard_auth_service as auth
from app.services.dashboard_auth_service import SessionIdentity

router = APIRouter(prefix="/auth", tags=["auth"])

_NO_STORE = {"Cache-Control": "no-store"}


def _session_read(request: Request, identity: SessionIdentity, token: str) -> SessionRead:
    settings = request.app.state.settings
    return SessionRead(
        user=SessionUserRead(
            id=identity.user_id, email=identity.email, display_name=identity.display_name
        ),
        organization=OrganizationRef(id=identity.organization_id, name=identity.organization_name),
        scopes=sorted(identity.scopes),
        csrf_token=auth.csrf_token_for(token),
        expires_at=identity.expires_at,
        environment=EnvironmentRead(
            app_env=settings.app_env, voice_provider=settings.voice_provider
        ),
    )


def _cookie_path(request: Request) -> str:
    return request.app.state.settings.api_v1_prefix


@router.post("/login")
def login(
    body: LoginRequest, request: Request, response: Response, session: DbSession
) -> SessionRead:
    settings = request.app.state.settings
    created = auth.login(session, settings, body.email, body.password)
    response.set_cookie(
        auth.SESSION_COOKIE,
        created.token,
        max_age=settings.dashboard_session_ttl_seconds,
        path=_cookie_path(request),
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    response.headers.update(_NO_STORE)
    return _session_read(request, created.identity, created.token)


@router.get("/session")
def current_session(request: Request, response: Response, session: DbSession) -> SessionRead:
    token = request.cookies.get(auth.SESSION_COOKIE, "")
    identity = auth.resolve_session(session, token)
    session.rollback()
    if identity is None:
        raise AuthenticationError("Not signed in.", code="session_invalid")
    response.headers.update(_NO_STORE)
    return _session_read(request, identity, token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, session: DbSession) -> Response:
    token = request.cookies.get(auth.SESSION_COOKIE, "")
    if token:
        if not auth.csrf_matches(token, request.headers.get(auth.CSRF_HEADER)):
            raise PermissionDeniedError("A valid CSRF token is required.", code="csrf_failed")
        auth.logout(session, token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT, headers=_NO_STORE)
    response.delete_cookie(
        auth.SESSION_COOKIE,
        path=_cookie_path(request),
        secure=request.app.state.settings.session_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response
