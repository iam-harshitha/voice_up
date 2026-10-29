from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

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
from app.services import approval_service, voice_service

router = APIRouter(prefix="/requests", tags=["requests"])

CanRead = Annotated[OrganizationContext, Depends(require_scope(Scope.REQUESTS_READ))]
CanCreate = Annotated[OrganizationContext, Depends(require_scope(Scope.REQUESTS_CREATE))]


# Opaque client-chosen key (e.g. a UUID). 8-128 characters from a safe alphabet.
IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:\-]+$",
        description=(
            "Optional. Retrying with the same key and payload returns the original request "
            "(200, Idempotent-Replayed: true) instead of creating a duplicate. The same key "
            "with a different payload is refused with 422."
        ),
    ),
]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "Replay of the request created with this Idempotency-Key."}},
)
def create_request(
    body: ApprovalRequestCreate,
    org: CanCreate,
    session: DbSession,
    http_request: Request,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> ApprovalRequestRead:
    settings = http_request.app.state.settings
    # Policies with auto_dial schedule their calls server-side, in the creation
    # transaction, so the agent's key never needs voice:calls:create.
    request, created = approval_service.submit_request(
        session,
        org.organization_id,
        body,
        idempotency_key=idempotency_key,
        on_created=lambda db, new, policy: voice_service.auto_dial(db, settings, new, policy),
    )
    if not created:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replayed"] = "true"
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
