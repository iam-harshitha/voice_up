"""Vote submission.

This is a trusted integration/testing interface: the caller asserts which approver is
voting, and the approver's identity is NOT independently verified here. Only issue the
votes:write scope to trusted operators. Chunk 2 replaces this for voice votes with an
authenticated provider-event flow that maps a verified call to its approver.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.dependencies import DbSession, OrganizationContext, require_scope
from app.core.security import Scope
from app.schemas.vote import VoteCreate, VoteRead, VoteRequestState, VoteResult
from app.services import approval_service

router = APIRouter(prefix="/requests", tags=["votes"])

CanVote = Annotated[OrganizationContext, Depends(require_scope(Scope.VOTES_WRITE))]


@router.post(
    "/{request_id}/votes",
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "Identical vote already recorded; nothing changed."}},
)
def cast_vote(
    request_id: UUID, body: VoteCreate, org: CanVote, session: DbSession, response: Response
) -> VoteResult:
    outcome = approval_service.cast_vote(
        session, org.organization_id, request_id, body.approver_id, body.vote
    )
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return VoteResult(
        vote=VoteRead.model_validate(outcome.vote),
        request=VoteRequestState(
            id=outcome.request.id,
            status=outcome.request.status,
            decided_at=outcome.request.decided_at,
        ),
        created=outcome.created,
    )
