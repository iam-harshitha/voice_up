"""Conditions, escalation status and audit timelines (organization-scoped)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.dependencies import DbSession, OrganizationContext, require_scope
from app.api.v1.presenters import evaluation_read
from app.core.security import Scope
from app.schemas.workflow import (
    AuditEventRead,
    AuditTimeline,
    ConditionCreate,
    ConditionRead,
    ConditionResolve,
    ConditionResult,
    EscalationStepRead,
    RecentAuditEvents,
    RequestStateRead,
)
from app.services import approval_service, audit_service, policy_service, voice_service
from app.services.approval_service import ConditionOutcome

router = APIRouter(tags=["workflow"])

CanReadRequests = Annotated[OrganizationContext, Depends(require_scope(Scope.REQUESTS_READ))]
# Recording a condition asserts what an approver said: same trust level as votes:write.
CanRecordDecisions = Annotated[OrganizationContext, Depends(require_scope(Scope.VOTES_WRITE))]
CanResolve = Annotated[OrganizationContext, Depends(require_scope(Scope.CONDITIONS_RESOLVE))]
CanReadAudit = Annotated[OrganizationContext, Depends(require_scope(Scope.AUDIT_READ))]

AuditAfter = Annotated[int, Query(ge=0)]
AuditLimit = Annotated[int, Query(ge=1, le=200)]
RecentLimit = Annotated[int, Query(ge=1, le=50)]


def _result(session, outcome: ConditionOutcome) -> ConditionResult:
    request = approval_service.get_request(
        session, outcome.request.organization_id, outcome.request.id
    )
    return ConditionResult(
        condition=ConditionRead.model_validate(outcome.condition),
        request=RequestStateRead(
            id=request.id, status=request.status, evaluation=evaluation_read(session, request)
        ),
        created=outcome.created,
    )


@router.post(
    "/requests/{request_id}/conditions",
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "Identical open condition already recorded."}},
)
def create_condition(
    request_id: UUID,
    body: ConditionCreate,
    org: CanRecordDecisions,
    session: DbSession,
    response: Response,
) -> ConditionResult:
    outcome = approval_service.create_condition(
        session, org.organization_id, request_id, body.approver_id, body.description
    )
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return _result(session, outcome)


@router.get("/requests/{request_id}/conditions")
def list_conditions(
    request_id: UUID, org: CanReadRequests, session: DbSession
) -> list[ConditionRead]:
    conditions = approval_service.list_conditions(session, org.organization_id, request_id)
    return [ConditionRead.model_validate(c) for c in conditions]


@router.post(
    "/requests/{request_id}/conditions/{condition_id}/resolve",
    responses={200: {"description": "Resolved (or the identical resolution already was)."}},
)
def resolve_condition(
    request_id: UUID,
    condition_id: UUID,
    body: ConditionResolve,
    org: CanResolve,
    session: DbSession,
    http_request: Request,
) -> ConditionResult:
    settings = http_request.app.state.settings
    outcome = approval_service.resolve_condition(
        session,
        org.organization_id,
        request_id,
        condition_id,
        outcome=body.outcome,
        note=body.note,
        evidence_reference=body.evidence_reference,
        actor_api_key_id=org.principal.api_key_id,
        on_satisfied=lambda db, request, approver_id: voice_service.schedule_reapproval(
            db, settings, request, approver_id
        ),
    )
    return _result(session, outcome)


@router.get("/requests/{request_id}/escalations")
def list_escalations(
    request_id: UUID, org: CanReadRequests, session: DbSession
) -> list[EscalationStepRead]:
    steps = approval_service.list_escalations(session, org.organization_id, request_id)
    return [EscalationStepRead.model_validate(step) for step in steps]


@router.get("/requests/{request_id}/audit-events")
def request_audit_events(
    request_id: UUID,
    org: CanReadAudit,
    session: DbSession,
    after: AuditAfter = 0,
    limit: AuditLimit = 100,
) -> AuditTimeline:
    approval_service.get_request(session, org.organization_id, request_id)  # 404 if foreign
    items, next_after = audit_service.timeline(
        session, org.organization_id, request_id=request_id, after=after, limit=limit
    )
    return AuditTimeline(
        items=[AuditEventRead.model_validate(e) for e in items], limit=limit, next_after=next_after
    )


@router.get("/audit-events/recent")
def recent_audit_events(
    org: CanReadAudit, session: DbSession, limit: RecentLimit = 20
) -> RecentAuditEvents:
    items = audit_service.recent(session, org.organization_id, limit=limit)
    return RecentAuditEvents(items=[AuditEventRead.model_validate(e) for e in items], limit=limit)


@router.get("/policies/{policy_id}/audit-events")
def policy_audit_events(
    policy_id: UUID,
    org: CanReadAudit,
    session: DbSession,
    after: AuditAfter = 0,
    limit: AuditLimit = 100,
) -> AuditTimeline:
    policy_service.get_policy(session, org.organization_id, policy_id)  # 404 if foreign
    items, next_after = audit_service.timeline(
        session, org.organization_id, policy_id=policy_id, after=after, limit=limit
    )
    return AuditTimeline(
        items=[AuditEventRead.model_validate(e) for e in items], limit=limit, next_after=next_after
    )
