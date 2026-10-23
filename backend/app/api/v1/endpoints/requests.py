from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import DbSession, OrganizationContext, PageParams, require_scope
from app.core.security import Scope
from app.domain.approval_rules import RequestStatus
from app.schemas.common import Page
from app.schemas.request import ApprovalRequestCreate, ApprovalRequestRead, ApprovalRequestSummary
from app.services import approval_service

router = APIRouter(prefix="/requests", tags=["requests"])

CanRead = Annotated[OrganizationContext, Depends(require_scope(Scope.REQUESTS_READ))]
CanCreate = Annotated[OrganizationContext, Depends(require_scope(Scope.REQUESTS_CREATE))]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_request(
    body: ApprovalRequestCreate, org: CanCreate, session: DbSession
) -> ApprovalRequestRead:
    request = approval_service.create_request(session, org.organization_id, body)
    return ApprovalRequestRead.model_validate(request)


@router.get("")
def list_requests(
    org: CanRead,
    session: DbSession,
    page: PageParams,
    status: Annotated[RequestStatus | None, Query()] = None,
) -> Page[ApprovalRequestSummary]:
    items, total = approval_service.list_requests(
        session, org.organization_id, limit=page.limit, offset=page.offset, status=status
    )
    return Page[ApprovalRequestSummary](
        items=[ApprovalRequestSummary.model_validate(item) for item in items],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{request_id}")
def get_request(request_id: UUID, org: CanRead, session: DbSession) -> ApprovalRequestRead:
    request = approval_service.get_request(session, org.organization_id, request_id)
    return ApprovalRequestRead.model_validate(request)
