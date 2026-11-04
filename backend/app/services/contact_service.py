"""How a new request's approvers are contacted, chosen by its policy's approval_channel.

- 'voice' (default): exactly voice_service.auto_dial, unchanged.
- 'text': Telegram prompts only; no call is ever placed, even with auto_dial on.
  (auto_dial still means "contact the approvers when the request is created", which is
  what the Claude Code guard checks for.)
"""

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import ApprovalPolicy, ApprovalRequest
from app.services import audit_service, text_service, voice_service
from app.services.audit_service import AuditEventType


def on_request_created(
    session: Session, settings: Settings, request: ApprovalRequest, policy: ApprovalPolicy
) -> None:
    if not text_service.is_text_request(request):
        voice_service.auto_dial(session, settings, request, policy)
        return
    if not policy.auto_dial:
        return
    try:
        with session.begin_nested():
            text_service.auto_prompt(session, settings, request)
    except SQLAlchemyError:
        # The request is still created (pending); the failure is audited.
        audit_service.record_event(
            session,
            organization_id=request.organization_id,
            request_id=request.id,
            event_type=AuditEventType.TEXT_PROMPT_FAILED,
            data={"reason": "scheduling_error", "stage": "auto_dial"},
        )
