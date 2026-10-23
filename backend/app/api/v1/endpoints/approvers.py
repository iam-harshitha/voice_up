from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.api.dependencies import DbSession, OrganizationContext, PageParams, require_scope
from app.core.security import Scope
from app.schemas.common import Page
from app.schemas.policy import ApproverCreate, ApproverRead, ApproverUpdate
from app.services import policy_service

router = APIRouter(prefix="/approvers", tags=["approvers"])

CanRead = Annotated[OrganizationContext, Depends(require_scope(Scope.APPROVERS_READ))]
CanWrite = Annotated[OrganizationContext, Depends(require_scope(Scope.APPROVERS_WRITE))]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_approver(body: ApproverCreate, org: CanWrite, session: DbSession) -> ApproverRead:
    approver = policy_service.create_approver(session, org.organization_id, body)
    return ApproverRead.model_validate(approver)


@router.get("")
def list_approvers(org: CanRead, session: DbSession, page: PageParams) -> Page[ApproverRead]:
    items, total = policy_service.list_approvers(
        session, org.organization_id, limit=page.limit, offset=page.offset
    )
    return Page[ApproverRead](
        items=[ApproverRead.model_validate(item) for item in items],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{approver_id}")
def get_approver(approver_id: UUID, org: CanRead, session: DbSession) -> ApproverRead:
    approver = policy_service.get_approver(session, org.organization_id, approver_id)
    return ApproverRead.model_validate(approver)


@router.patch("/{approver_id}")
def update_approver(
    approver_id: UUID, body: ApproverUpdate, org: CanWrite, session: DbSession
) -> ApproverRead:
    approver = policy_service.update_approver(session, org.organization_id, approver_id, body)
    return ApproverRead.model_validate(approver)
