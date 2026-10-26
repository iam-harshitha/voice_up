from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import DbSession, OrganizationContext, PageParams, require_scope
from app.api.v1.presenters import request_read, request_summary
from app.core.security import Scope
from app.domain.approval_rules import RequestStatus
from app.schemas.common import Page
from app.schemas.request import (
    ApprovalRequestCreate,
    ApprovalRequestRead,
    ApprovalRequestSummary,
    RequestStats,
    RequestStatusCounts,
)
from app.services import approval_service

router = APIRouter(prefix="/requests", tags=["requests"])

CanRead = Annotated[OrganizationContext, Depends(require_scope(Scope.REQUESTS_READ))]
CanCreate = Annotated[OrganizationContext, Depends(require_scope(Scope.REQUESTS_CREATE))]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_request(
    body: ApprovalRequestCreate, org: CanCreate, session: DbSession
) -> ApprovalRequestRead:
    request = approval_service.create_request(session, org.organization_id, body)
    return request_read(session, request)


@router.get("")
def list_requests(
    org: CanRead,
    session: DbSession,
    page: PageParams,
    status: Annotated[RequestStatus | None, Query()] = None,
    include_evaluation: Annotated[bool, Query()] = False,
) -> Page[ApprovalRequestSummary]:
    items, total = approval_service.list_requests(
        session, org.organization_id, limit=page.limit, offset=page.offset, status=status
    )
    return Page[ApprovalRequestSummary](
        items=[
            request_summary(session, item, include_evaluation=include_evaluation) for item in items
        ],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


# Declared before /{request_id} so "stats" is never parsed as a request ID.
@router.get("/stats")
def request_stats(org: CanRead, session: DbSession) -> RequestStats:
    stats = approval_service.request_stats(session, org.organization_id)
    return RequestStats(
        total=stats.total,
        by_status=RequestStatusCounts(**stats.by_status),
        pending_waiting_for_condition=stats.waiting_for_condition,
        pending_awaiting_reapproval=stats.awaiting_reapproval,
        pending_deadline_passed=stats.deadline_passed,
        pending_evaluated=stats.pending_evaluated,
        pending_evaluation_limit=approval_service.STATS_PENDING_EVALUATION_LIMIT,
        evaluation_complete=stats.pending_evaluated >= stats.pending_total,
    )


@router.get("/{request_id}")
def get_request(request_id: UUID, org: CanRead, session: DbSession) -> ApprovalRequestRead:
    request = approval_service.get_request(session, org.organization_id, request_id)
    return request_read(session, request)
