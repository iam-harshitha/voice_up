from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, StringConstraints

from app.schemas.common import StrictInput


class LoginRequest(StrictInput):
    email: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=320)]
    # Not stripped: whitespace may be part of a password.
    password: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class SessionUserRead(BaseModel):
    id: UUID
    email: str
    display_name: str


class OrganizationRef(BaseModel):
    id: UUID
    name: str


class EnvironmentRead(BaseModel):
    """Lets the dashboard label simulated voice activity honestly."""

    app_env: str
    voice_provider: str  # disabled | mock (simulated calls) | bolna (real calls)


class SessionRead(BaseModel):
    user: SessionUserRead
    organization: OrganizationRef
    scopes: list[str]
    # Send back as X-CSRF-Token on POST/PATCH requests made with the session cookie.
    csrf_token: str
    expires_at: datetime
    environment: EnvironmentRead
