import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import DbSession
from app.core.errors import error_body

router = APIRouter(prefix="/health", tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", responses={503: {"description": "Database unreachable"}})
def ready(session: DbSession) -> JSONResponse:
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        # Log only the exception type: driver messages can include host and user names.
        logger.warning("health.database_unavailable")
        return JSONResponse(
            status_code=503,
            content=error_body("database_unavailable", "The database is not reachable."),
        )
    return JSONResponse({"status": "ok", "database": "ok"})
