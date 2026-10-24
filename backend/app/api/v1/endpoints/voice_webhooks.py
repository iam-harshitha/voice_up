"""Provider webhook (provider-to-server; not API-key authenticated).

Bolna signs nothing and sends no event ID. Its documented controls are custom headers on
webhook deliveries and a fixed set of source IPs. We therefore require a high-entropy
shared token in a custom header (compared in constant time) and recommend enforcing the
IP allowlist at the edge. Even with a leaked token, a forged body cannot create a vote:
decisions are read only from the execution re-fetched from Bolna's API with our key.
"""

import hmac
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.core.errors import error_body
from app.services.voice_webhook_service import InvalidWebhookError, ingest_webhook

router = APIRouter(prefix="/voice/webhooks", tags=["voice"])
logger = logging.getLogger(__name__)

WEBHOOK_TOKEN_HEADER = "X-Approval-Line-Webhook-Token"  # noqa: S105 - header name


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content=error_body(code, message))


@router.post(
    "/bolna",
    responses={
        200: {"description": "Accepted, duplicate or ignored (no retry needed)."},
        401: {"description": "Missing or invalid webhook token."},
        503: {"description": "Temporarily unable to store the event; retry."},
    },
)
async def bolna_webhook(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    token = settings.voice_webhook_token
    if settings.voice_provider == "disabled" or token is None:
        return _error(404, "not_found", "Not Found")

    supplied = request.headers.get(WEBHOOK_TOKEN_HEADER, "")
    if not hmac.compare_digest(supplied.encode(), token.get_secret_value().encode()):
        logger.warning("voice.webhook_auth_failed", extra={"header_present": bool(supplied)})
        return _error(401, "webhook_unauthorized", "The webhook token is invalid.")

    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        return _error(415, "unsupported_media_type", "Content-Type must be application/json.")

    limit = settings.voice_webhook_max_bytes
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        return _error(413, "payload_too_large", "The webhook body is too large.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return _error(413, "payload_too_large", "The webhook body is too large.")

    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return _error(400, "invalid_payload", "The webhook body is not valid JSON.")

    def store():
        with request.app.state.session_factory() as session:
            return ingest_webhook(session, settings, settings.voice_provider, payload)

    try:
        result = await run_in_threadpool(store)
    except InvalidWebhookError:
        return _error(400, "invalid_payload", "The webhook payload is not a valid event.")
    except SQLAlchemyError:
        # Nothing was committed; the provider (or reconciliation polling) must retry.
        logger.exception("voice.webhook_store_failed")
        return _error(503, "temporarily_unavailable", "The event could not be stored.")
    return JSONResponse({"status": result.outcome})
