from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Request, Response, status

from app.api.dependencies import DbSession, OrganizationContext, require_scope
from app.core.security import Scope
from app.schemas.voice import (
    SkippedApprover,
    VoiceCallList,
    VoiceCallRead,
    VoiceScheduleCreate,
    VoiceScheduleResult,
)
from app.services import voice_service

router = APIRouter(prefix="/requests", tags=["voice"])

CanCreate = Annotated[OrganizationContext, Depends(require_scope(Scope.VOICE_CALLS_CREATE))]
CanRead = Annotated[OrganizationContext, Depends(require_scope(Scope.VOICE_CALLS_READ))]


@router.post(
    "/{request_id}/voice-calls",
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "Calls already scheduled; nothing new created."},
        503: {"description": "Voice calls are not enabled."},
    },
)
def schedule_voice_calls(
    request_id: UUID,
    org: CanCreate,
    session: DbSession,
    http_request: Request,
    response: Response,
    body: Annotated[VoiceScheduleCreate | None, Body()] = None,
) -> VoiceScheduleResult:
    result = voice_service.schedule_calls(
        session,
        http_request.app.state.settings,
        org.organization_id,
        request_id,
        redial_unanswered=bool(body and body.redial_unanswered),
    )
    if result.created == 0:
        response.status_code = status.HTTP_200_OK
    return VoiceScheduleResult(
        request_id=request_id,
        created=result.created,
        calls=[VoiceCallRead.model_validate(call) for call in result.calls],
        skipped=[SkippedApprover.model_validate(item) for item in result.skipped],
    )


@router.get("/{request_id}/voice-calls")
def list_voice_calls(request_id: UUID, org: CanRead, session: DbSession) -> VoiceCallList:
    calls = voice_service.list_calls(session, org.organization_id, request_id)
    return VoiceCallList(
        request_id=request_id, calls=[VoiceCallRead.model_validate(c) for c in calls]
    )
