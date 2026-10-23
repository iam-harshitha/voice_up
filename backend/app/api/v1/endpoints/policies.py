from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import DbSession, OrganizationContext, PageParams, require_scope
from app.core.security import Scope
from app.schemas.common import Page
from app.schemas.policy import PolicyCreate, PolicyRead, PolicyUpdate
from app.services import policy_service

router = APIRouter(prefix="/policies", tags=["policies"])

CanRead = Annotated[OrganizationContext, Depends(require_scope(Scope.POLICIES_READ))]
CanWrite = Annotated[OrganizationContext, Depends(require_scope(Scope.POLICIES_WRITE))]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_policy(body: PolicyCreate, org: CanWrite, session: DbSession) -> PolicyRead:
    policy = policy_service.create_policy(session, org.organization_id, body)
    return PolicyRead.model_validate(policy)


@router.get("")
def list_policies(
    org: CanRead,
    session: DbSession,
    page: PageParams,
    is_active: Annotated[bool | None, Query()] = None,
) -> Page[PolicyRead]:
    items, total = policy_service.list_policies(
        session, org.organization_id, limit=page.limit, offset=page.offset, is_active=is_active
    )
    return Page[PolicyRead](
        items=[PolicyRead.model_validate(item) for item in items],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{policy_id}")
def get_policy(policy_id: UUID, org: CanRead, session: DbSession) -> PolicyRead:
    policy = policy_service.get_policy(session, org.organization_id, policy_id)
    return PolicyRead.model_validate(policy)


@router.patch("/{policy_id}")
def update_policy(
    policy_id: UUID, body: PolicyUpdate, org: CanWrite, session: DbSession
) -> PolicyRead:
    policy = policy_service.update_policy(session, org.organization_id, policy_id, body)
    return PolicyRead.model_validate(policy)
