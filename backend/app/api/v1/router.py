from fastapi import APIRouter

from app.api.v1.endpoints import (
    approvers,
    health,
    policies,
    requests,
    voice_calls,
    voice_webhooks,
    votes,
)
from app.schemas.common import ErrorResponse

# Documented error shape for every non-health endpoint.
_ERRORS = {code: {"model": ErrorResponse} for code in (400, 401, 403, 404, 409, 422)}

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(approvers.router, responses=_ERRORS)
api_router.include_router(policies.router, responses=_ERRORS)
api_router.include_router(requests.router, responses=_ERRORS)
api_router.include_router(votes.router, responses=_ERRORS)
api_router.include_router(voice_calls.router, responses=_ERRORS)
api_router.include_router(voice_webhooks.router)
