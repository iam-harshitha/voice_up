"""How a new request's approvers are contacted, chosen by its policy's approval_channel.

- 'voice' (default): exactly voice_service.auto_dial, unchanged.
- 'text': Telegram prompts only; no call is ever placed, even with auto_dial on.
- 'text_then_voice': a Telegram prompt now and, for each approver who got one, a call
  delayed by voice_fallback_after_seconds (cancelled by the worker if the request or the
  approver's seat is decided first). Approvers with no verified link, or when text is
  off, are called immediately, exactly as with 'voice'.

auto_dial still means "contact the approvers when the request is created", which is
what the Claude Code guard checks for.
"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import ApprovalPolicy, ApprovalRequest
from app.db.models.text import PromptReason
from app.domain.snapshot import snapshot_channel
from app.services import audit_service, text_service, voice_service
from app.services.audit_service import AuditEventType


def _audit_scheduling_error(session: Session, request: ApprovalRequest) -> None:
    audit_service.record_event(
        session,
        organization_id=request.organization_id,
        request_id=request.id,
        event_type=AuditEventType.TEXT_PROMPT_FAILED,
        data={"reason": "scheduling_error", "stage": "auto_dial"},
    )


def on_request_created(
    session: Session, settings: Settings, request: ApprovalRequest, policy: ApprovalPolicy
) -> None:
    channel = snapshot_channel(request.policy_snapshot)
    if channel == "voice":
        voice_service.auto_dial(session, settings, request, policy)
        return
    if not policy.auto_dial:
        return
    if channel == "text":
        try:
            with session.begin_nested():
                text_service.auto_prompt(session, settings, request)
        except SQLAlchemyError:
            # The request is still created (pending); the failure is audited.
            _audit_scheduling_error(session, request)
        return

    # text_then_voice
    start_after: dict[UUID, datetime] = {}
    fallback_at = utcnow() + timedelta(seconds=policy.voice_fallback_after_seconds)
    try:
        with session.begin_nested():
            for approver_id in request.authorized_approver_ids:  # snapshot order
                prompt, skipped = text_service.prompt_or_audit(
                    session, settings, request, approver_id, PromptReason.AUTO_DIAL
                )
                if prompt is not None and skipped is None:
                    start_after[approver_id] = fallback_at
    except SQLAlchemyError:
        start_after = {}  # no prompts: everyone is called immediately
        _audit_scheduling_error(session, request)
    voice_service.auto_dial(session, settings, request, policy, start_after=start_after)
