"""Text (Telegram) approval prompts: queueing, sending, and the tap decision transaction.

A prompt is one message with Approve / Reject buttons to one snapshotted approver. The
text worker (app/workers/text_worker.py) drives it:

  send       phase 1 (transaction): claim, mint a nonce, store only its hash, "sending"
             phase 2 (no locks):    provider.send_message
             phase 3 (transaction): "sent" + text.prompt_sent, or retry / "failed"
  tap        handle_tap(): every check, then the vote, in ONE transaction that locks the
             request first and the prompt second (the order votes and the voice webhook
             use), exactly like voice_webhook_service.apply_execution
  sweep      close prompts whose request was decided or whose buttons expired

Checks for a tap, in order: signature; prompt exists and is unused (in the status its
button belongs to); not expired; sender's Telegram user ID and chat match the verified
link; request still pending. Only then is the prompt consumed and
approval_service.record_vote(source="telegram") called. The policy engine still decides
the request; a tap is only a vote. Approve needs a second, "Confirm approve" tap.

Provider calls never happen inside a transaction: handlers return Actions that the
worker performs after commit.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import AppError
from app.db.base import utcnow
from app.db.models import (
    ApprovalRequest,
    Approver,
    ApproverChannelLink,
    TextPrompt,
    VoiceCall,
    VoiceJob,
)
from app.db.models.text import LinkStatus, PromptReason, PromptStatus
from app.db.models.voice import JobStatus, JobType
from app.db.session import transaction
from app.domain.approval_rules import RequestStatus, VoteChoice
from app.domain.snapshot import snapshot_channel
from app.services import approval_service, audit_service, channel_link_service
from app.services.audit_service import AuditEventType
from app.text import message as msg
from app.text import tokens
from app.text.base import (
    Button,
    CallbackTap,
    ErrorKind,
    IncomingText,
    OutgoingMessage,
    TextProvider,
    TextProviderError,
    Update,
)
from app.voice.base import CallStatus

logger = logging.getLogger(__name__)

SEND_LEASE = timedelta(seconds=120)
RETRY_BASE_SECONDS = 5.0


def is_text_request(request: ApprovalRequest) -> bool:
    return snapshot_channel(request.policy_snapshot) == "text"


def _secret(settings: Settings) -> str:
    secret = settings.text_callback_secret
    if secret is None:  # Settings refuses an enabled provider without one
        raise RuntimeError("TEXT_CALLBACK_SECRET is not set")
    return secret.get_secret_value()


def _audit(session: Session, prompt: TextPrompt, event: AuditEventType, **data: Any) -> None:
    audit_service.record_event(
        session,
        organization_id=prompt.organization_id,
        request_id=prompt.request_id,
        event_type=event,
        data={"text_prompt_id": str(prompt.id), "approver_id": str(prompt.approver_id), **data},
    )


# --- Queueing (inside the caller's transaction, request row locked or just inserted) ---


def queue_prompt(
    session: Session,
    settings: Settings,
    request: ApprovalRequest,
    approver_id: UUID,
    *,
    reason: str,
    now: datetime | None = None,
) -> tuple[TextPrompt | None, str | None]:
    """Create one prompt for one approver. Returns (prompt, None), or (existing or None,
    skip reason). Used by auto-dial, escalation and re-approval of 'text' requests."""
    now = now or utcnow()
    if settings.text_provider == "disabled":
        return None, "text_unavailable"
    existing = session.scalar(
        select(TextPrompt).where(
            TextPrompt.request_id == request.id,
            TextPrompt.approver_id == approver_id,
            TextPrompt.status.in_(PromptStatus.OPEN),
        )
    )
    if existing is not None:
        return existing, "already_prompted"
    approver = session.scalar(
        select(Approver).where(
            Approver.id == approver_id, Approver.organization_id == request.organization_id
        )
    )
    if approver is None or not approver.is_active:
        return None, "approver_inactive"
    link = channel_link_service.verified_link(session, approver_id)
    if link is None or link.organization_id != request.organization_id:
        return None, "no_verified_link"
    expires_at = now + timedelta(seconds=settings.text_prompt_ttl_seconds)
    if request.expires_at is not None:
        expires_at = min(expires_at, request.expires_at)
    if expires_at <= now:
        return None, "request_expired"
    prompt = TextPrompt(
        organization_id=request.organization_id,
        request_id=request.id,
        approver_id=approver_id,
        link_id=link.id,
        provider=settings.text_provider,
        reason=reason,
        status=PromptStatus.QUEUED,
        run_after=now,
        expires_at=expires_at,
    )
    session.add(prompt)
    session.flush()
    return prompt, None


def prompt_or_audit(
    session: Session, settings: Settings, request: ApprovalRequest, approver_id: UUID, reason: str
) -> tuple[TextPrompt | None, str | None]:
    """queue_prompt, auditing text.prompt_failed when no new prompt can be created."""
    prompt, skipped = queue_prompt(session, settings, request, approver_id, reason=reason)
    if skipped is not None and skipped != "already_prompted":
        audit_service.record_event(
            session,
            organization_id=request.organization_id,
            request_id=request.id,
            event_type=AuditEventType.TEXT_PROMPT_FAILED,
            data={"approver_id": str(approver_id), "reason": skipped, "stage": reason},
        )
    return prompt, skipped


def auto_prompt(session: Session, settings: Settings, request: ApprovalRequest) -> None:
    """Text counterpart of voice auto-dial: one prompt per snapshotted primary approver.
    Runs inside the request's creation transaction; never places a call."""
    for approver_id in request.authorized_approver_ids:  # snapshot order
        prompt_or_audit(session, settings, request, approver_id, PromptReason.AUTO_DIAL)


def _expedite_fallback_call(
    session: Session, request: ApprovalRequest, prompt: TextPrompt, now: datetime, reason: str
) -> None:
    """For a text_then_voice request, move the approver's delayed call to now."""
    if snapshot_channel(request.policy_snapshot) != "text_then_voice":
        return
    job = session.scalar(
        select(VoiceJob)
        .join(VoiceCall, VoiceJob.voice_call_id == VoiceCall.id)
        .where(
            VoiceCall.request_id == prompt.request_id,
            VoiceCall.approver_id == prompt.approver_id,
            VoiceCall.status == CallStatus.QUEUED.value,
            VoiceJob.job_type == JobType.INITIATE_CALL,
            VoiceJob.status == JobStatus.PENDING,
            VoiceJob.run_after > now,
        )
        # A job a worker holds is already running: leave it alone.
        .with_for_update(of=VoiceJob, skip_locked=True)
        .limit(1)
    )
    if job is None:
        return
    job.run_after = now
    audit_service.record_event(
        session,
        organization_id=prompt.organization_id,
        request_id=prompt.request_id,
        event_type=AuditEventType.VOICE_FALLBACK_EXPEDITED,
        data={
            "voice_call_id": str(job.voice_call_id),
            "text_prompt_id": str(prompt.id),
            "approver_id": str(prompt.approver_id),
            "reason": reason,
        },
    )


# --- Worker actions (performed after commit) ----------------------------------------------


@dataclass(frozen=True)
class AnswerCallback:
    callback_id: str
    text: str


@dataclass(frozen=True)
class EditMessage:
    chat_id: str
    message_id: str
    text: str
    buttons: tuple[Button, ...] = ()


@dataclass(frozen=True)
class SendText:
    chat_id: str
    text: str


Action = AnswerCallback | EditMessage | SendText


@dataclass
class TapResult:
    accepted: bool
    reason: str
    actions: list[Action] = field(default_factory=list)


def perform(provider: TextProvider, actions: list[Action]) -> None:
    """Best effort: a failed answer or edit never undoes a committed decision."""
    for action in actions:
        try:
            if isinstance(action, AnswerCallback):
                provider.answer_callback(action.callback_id, action.text)
            elif isinstance(action, EditMessage):
                provider.edit_message(
                    action.chat_id, action.message_id, action.text, action.buttons
                )
            else:
                provider.send_message(OutgoingMessage(action.chat_id, action.text))
        except TextProviderError as exc:
            logger.warning(
                "text.action_failed",
                extra={"action": type(action).__name__, "error": exc.code, "kind": exc.kind},
            )


# --- Sending ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Prepared:
    prompt_id: UUID
    token_hash: str
    message: OutgoingMessage


def _load_message_parts(session: Session, prompt: TextPrompt):
    request = session.get(ApprovalRequest, prompt.request_id)
    approver = session.get(Approver, prompt.approver_id)
    link = session.get(ApproverChannelLink, prompt.link_id)
    if request is None or approver is None or link is None:  # pragma: no cover - RESTRICT FKs
        raise RuntimeError("prompt references a missing row")
    return request, approver, link


def _closed_reason(request: ApprovalRequest, prompt: TextPrompt, now: datetime) -> str | None:
    if request.status != RequestStatus.PENDING.value:
        return "request_closed"
    if request.expires_at is not None and request.expires_at <= now:
        return "request_closed"
    if prompt.expires_at <= now:
        return "expired"
    return None


CLOSED_INSTEAD = "closed_instead"


def _claim_for_send(session: Session, settings: Settings, now: datetime) -> _Prepared | str | None:
    """Phase 1. Returns a prepared send, CLOSED_INSTEAD if the claimed prompt was closed
    instead of sent, or None when nothing is due."""
    with transaction(session):
        prompt = session.scalar(
            select(TextPrompt)
            .where(
                or_(
                    (TextPrompt.status == PromptStatus.QUEUED) & (TextPrompt.run_after <= now),
                    (TextPrompt.status == PromptStatus.SENDING)
                    & (TextPrompt.lease_expires_at < now),
                )
            )
            .order_by(TextPrompt.run_after, TextPrompt.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if prompt is None:
            return None
        request, approver, link = _load_message_parts(session, prompt)
        closed = _closed_reason(request, prompt, now)
        if closed is None and link.status != LinkStatus.VERIFIED:
            closed = "link_revoked"
        if closed is not None:
            prompt.status = PromptStatus.CLOSED
            prompt.outcome = closed
            prompt.lease_expires_at = None
            if closed == "link_revoked":
                _audit(session, prompt, AuditEventType.TEXT_PROMPT_FAILED, reason=closed)
                _expedite_fallback_call(session, request, prompt, now, closed)
            return CLOSED_INSTEAD
        nonce = tokens.new_nonce()
        prompt.token_hash = tokens.sha256_hex(nonce)
        prompt.confirm_token_hash = None
        prompt.status = PromptStatus.SENDING
        prompt.lease_expires_at = now + SEND_LEASE
        prompt.attempts += 1
        prompt.chat_id = link.external_chat_id
        message = OutgoingMessage(
            chat_id=link.external_chat_id or "",
            text=msg.prompt_text(request, approver, prompt.expires_at),
            buttons=msg.first_stage_buttons(_secret(settings), nonce),
        )
        return _Prepared(prompt.id, prompt.token_hash, message)


def _finish_send(
    session: Session,
    settings: Settings,
    prepared: _Prepared,
    message_id: str | None,
    error: TextProviderError | None,
) -> str:
    """Phase 3."""
    now = utcnow()
    with transaction(session):
        prompt = session.scalar(
            select(TextPrompt).where(TextPrompt.id == prepared.prompt_id).with_for_update()
        )
        if (
            prompt is None
            or prompt.status != PromptStatus.SENDING
            or prompt.token_hash != prepared.token_hash
        ):
            return "lease_lost"  # another worker took the prompt over
        prompt.lease_expires_at = None
        if error is None and message_id is not None:
            prompt.status = PromptStatus.SENT
            prompt.provider_message_id = message_id[:32]
            prompt.sent_at = now
            prompt.last_error = None
            _audit(
                session,
                prompt,
                AuditEventType.TEXT_PROMPT_SENT,
                reason=prompt.reason,
                attempt=prompt.attempts,
                provider=prompt.provider,
                expires_at=prompt.expires_at.isoformat(),
            )
            return "sent"
        code = error.code if error is not None else "no_message_id"
        prompt.last_error = code
        # A message that may have been delivered keeps no working buttons: the stored
        # hash is dropped and the next attempt mints a new one.
        prompt.token_hash = None
        # text_then_voice: the text did not arrive, so the fallback call starts now
        # (later retries of the message may still deliver it; the first vote wins).
        request = session.get(ApprovalRequest, prompt.request_id)
        if request is not None:
            _expedite_fallback_call(session, request, prompt, now, code)
        retryable = error is None or error.kind is ErrorKind.RETRYABLE
        if retryable and prompt.attempts < settings.text_send_max_attempts:
            prompt.status = PromptStatus.QUEUED
            prompt.run_after = now + timedelta(
                seconds=RETRY_BASE_SECONDS * 2 ** (prompt.attempts - 1)
            )
            return "retry"
        prompt.status = PromptStatus.FAILED
        prompt.outcome = code[:48]
        _audit(
            session,
            prompt,
            AuditEventType.TEXT_PROMPT_FAILED,
            reason=code,
            attempts=prompt.attempts,
        )
        return "failed"


def send_due_prompts(
    session_factory: sessionmaker[Session],
    provider: TextProvider,
    settings: Settings,
    *,
    now: datetime | None = None,
    limit: int = 20,
) -> int:
    """Send up to ``limit`` due prompts. Returns how many prompts were handled."""
    handled = 0
    while handled < limit:
        with session_factory() as session:
            prepared = _claim_for_send(session, settings, now or utcnow())
        if prepared is None:
            break
        handled += 1
        if not isinstance(prepared, _Prepared):
            continue  # closed instead of sent
        message_id: str | None = None
        error: TextProviderError | None = None
        try:
            message_id = provider.send_message(prepared.message)
        except TextProviderError as exc:
            error = exc
        with session_factory() as session:
            result = _finish_send(session, settings, prepared, message_id, error)
        logger.info(
            "text.prompt_send",
            extra={"text_prompt_id": str(prepared.prompt_id), "result": result},
        )
    return handled


# --- Taps -------------------------------------------------------------------------------------


def _reject(
    session: Session | None,
    prompt: TextPrompt | None,
    tap: CallbackTap,
    reason: str,
    action: str | None,
    extra: list[Action] | None = None,
) -> TapResult:
    if session is not None and prompt is not None:
        _audit(
            session,
            prompt,
            AuditEventType.TEXT_DECISION_REJECTED,
            reason=reason,
            button=action,
        )
    logger.info("text.tap_rejected", extra={"reason": reason})
    return TapResult(
        False, reason, [AnswerCallback(tap.callback_id, msg.NOT_ACCEPTED), *(extra or [])]
    )


def _close_and_edit(
    session: Session, prompt: TextPrompt, outcome: str, note_key: str
) -> list[Action]:
    """Close an open prompt and return the edit that removes its buttons."""
    if prompt.status in PromptStatus.OPEN:
        prompt.status = PromptStatus.CLOSED
        prompt.outcome = outcome
        prompt.lease_expires_at = None
    return _final_edit(session, prompt, msg.OUTCOME_NOTES[note_key])


def _final_edit(session: Session, prompt: TextPrompt, note: str) -> list[Action]:
    if prompt.chat_id is None or prompt.provider_message_id is None:
        return []
    request, approver, _ = _load_message_parts(session, prompt)
    text = msg.with_note(msg.prompt_text(request, approver, prompt.expires_at), note)
    return [EditMessage(prompt.chat_id, prompt.provider_message_id, text, ())]


def _find_prompt_id(session: Session, action: str, nonce: str) -> UUID | None:
    column = (
        TextPrompt.token_hash if action in tokens.FIRST_STAGE else TextPrompt.confirm_token_hash
    )
    return session.scalar(select(TextPrompt.id).where(column == tokens.sha256_hex(nonce)))


def _find_prompt_by_any_nonce(session: Session, nonce: str) -> UUID | None:
    digest = tokens.sha256_hex(nonce)
    return session.scalar(
        select(TextPrompt.id).where(
            or_(TextPrompt.token_hash == digest, TextPrompt.confirm_token_hash == digest)
        )
    )


def handle_tap(
    session: Session, settings: Settings, tap: CallbackTap, *, now: datetime | None = None
) -> TapResult:
    # 1. Signature (no database access before this passes).
    verified = tokens.verify(_secret(settings), tap.data)
    if verified is None:
        parsed = tokens.parse(tap.data)
        # Attribute a tampered token to its prompt when the nonce is real (e.g. a swapped
        # action), so the attempt appears in that request's audit trail.
        with transaction(session):
            prompt_id = _find_prompt_by_any_nonce(session, parsed[1]) if parsed else None
            prompt = session.get(TextPrompt, prompt_id) if prompt_id else None
            return _reject(session, prompt, tap, "bad_signature", parsed[0] if parsed else None)
    action, nonce = verified

    # 2. The prompt exists (lookup only; re-read under lock below).
    prompt_id = _find_prompt_id(session, action, nonce)
    session.rollback()
    if prompt_id is None:
        return _reject(None, None, tap, "unknown_token", action)

    with transaction(session):
        # Lock order: request, then prompt (as votes and the voice webhook do).
        request_id = session.scalar(select(TextPrompt.request_id).where(TextPrompt.id == prompt_id))
        request = session.scalar(
            select(ApprovalRequest)
            .where(ApprovalRequest.id == request_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        prompt = session.scalar(
            select(TextPrompt)
            .where(TextPrompt.id == prompt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if request is None or prompt is None:  # pragma: no cover - RESTRICT foreign keys
            return _reject(None, None, tap, "unknown_token", action)
        now = now or utcnow()

        # 2b. ...and is unused: its button must belong to the prompt's current stage.
        expected = PromptStatus.SENT if action in tokens.FIRST_STAGE else PromptStatus.CONFIRMING
        stored = prompt.token_hash if action in tokens.FIRST_STAGE else prompt.confirm_token_hash
        if prompt.status != expected or stored != tokens.sha256_hex(nonce):
            return _reject(session, prompt, tap, "token_used", action)

        # 3. Not expired.
        if prompt.expires_at <= now:
            edits = _close_and_edit(session, prompt, "expired", "expired")
            return _reject(session, prompt, tap, "token_expired", action, edits)

        # 4. The sender is the verified account, in its own chat, on this message.
        link = session.get(ApproverChannelLink, prompt.link_id, with_for_update={"read": True})
        if link is None or link.status != LinkStatus.VERIFIED:
            return _reject(session, prompt, tap, "link_revoked", action)
        if tap.sender_id != link.external_user_id:
            return _reject(session, prompt, tap, "sender_mismatch", action)
        if (
            tap.chat_id is None
            or tap.chat_id != link.external_chat_id
            or tap.chat_id != prompt.chat_id
            or tap.message_id != prompt.provider_message_id
        ):
            return _reject(session, prompt, tap, "wrong_chat", action)

        # 5. The request is still pending (the deadline is checked again by record_vote).
        if _closed_reason(request, prompt, now) == "request_closed":
            edits = _close_and_edit(session, prompt, "request_closed", "closed")
            return _reject(session, prompt, tap, "request_closed", action, edits)

        secret = _secret(settings)
        request_text = msg.prompt_text(
            request, session.get(Approver, prompt.approver_id), prompt.expires_at
        )
        chat_id, message_id = prompt.chat_id or "", prompt.provider_message_id or ""

        if action == tokens.APPROVE:  # first tap: ask for confirmation (no vote yet)
            confirm_nonce = tokens.new_nonce()
            prompt.status = PromptStatus.CONFIRMING
            prompt.confirm_token_hash = tokens.sha256_hex(confirm_nonce)
            return TapResult(
                True,
                "confirmation_requested",
                [
                    AnswerCallback(tap.callback_id, msg.CONFIRM_NOTE),
                    EditMessage(
                        chat_id,
                        message_id,
                        msg.with_note(request_text, msg.CONFIRM_NOTE),
                        msg.confirm_buttons(secret, confirm_nonce),
                    ),
                ],
            )
        if action == tokens.CANCEL:  # back to Approve / Reject with fresh tokens
            fresh = tokens.new_nonce()
            prompt.status = PromptStatus.SENT
            prompt.token_hash = tokens.sha256_hex(fresh)
            prompt.confirm_token_hash = None
            return TapResult(
                True,
                "confirmation_cancelled",
                [
                    AnswerCallback(tap.callback_id, msg.CANCELLED_NOTE),
                    EditMessage(
                        chat_id, message_id, request_text, msg.first_stage_buttons(secret, fresh)
                    ),
                ],
            )

        # Reject, or Confirm approve: consume the prompt and vote, atomically.
        choice = VoteChoice.APPROVE if action == tokens.CONFIRM else VoteChoice.REJECT
        prompt.status = PromptStatus.ANSWERED
        prompt.answered_at = now
        prompt.decision = choice.value
        try:
            # Savepoint: if the approval service refuses, the consumed prompt still commits.
            with session.begin_nested():
                outcome = approval_service.record_vote(
                    session,
                    prompt.organization_id,
                    prompt.request_id,
                    prompt.approver_id,
                    choice,
                    source="telegram",
                    source_details={"text_prompt_id": str(prompt.id)},
                )
        except (AppError, IntegrityError) as exc:
            code = exc.code if isinstance(exc, AppError) else "vote_conflict"
            prompt.outcome = code[:48]
            edits = _final_edit(session, prompt, msg.OUTCOME_NOTES["refused"])
            return _reject(session, prompt, tap, code, action, edits)
        prompt.vote_id = outcome.vote.id
        prompt.outcome = "vote_recorded" if outcome.created else "vote_already_recorded"
        _audit(
            session,
            prompt,
            AuditEventType.TEXT_DECISION_ACCEPTED,
            vote=choice.value,
            vote_id=str(outcome.vote.id),
            request_status=outcome.request.status,
        )
        actions: list[Action] = [
            AnswerCallback(tap.callback_id, msg.ANSWERS[choice.value]),
            *_final_edit(session, prompt, msg.OUTCOME_NOTES[choice.value]),
        ]
    logger.info(
        "text.decision_recorded",
        extra={"text_prompt_id": str(prompt_id), "vote": choice.value},
    )
    return TapResult(True, prompt.outcome or "vote_recorded", actions)


def handle_text(session: Session, update: IncomingText) -> TapResult:
    """A typed message: /start pairing, or anything else (never counted as a decision)."""
    if update.chat_type != "private":
        return TapResult(False, "ignored_non_private")
    command, _, argument = update.text.strip().partition(" ")
    if command == "/start":
        if not argument.strip():
            return TapResult(
                False,
                "start_without_code",
                [
                    SendText(
                        update.chat_id, "To link this chat, open the pairing link you were given."
                    )
                ],
            )
        result = channel_link_service.complete_pairing(
            session,
            code=argument.strip(),
            sender_id=update.sender_id,
            chat_id=update.chat_id,
            chat_type=update.chat_type,
        )
        return TapResult(result.ok, result.reason, [SendText(update.chat_id, result.reply)])
    logger.info("text.free_text_ignored", extra={"reason": "free_text"})
    return TapResult(False, "free_text", [SendText(update.chat_id, msg.FREE_TEXT_REPLY)])


def process_update(
    session_factory: sessionmaker[Session],
    provider: TextProvider,
    settings: Settings,
    update: Update,
    *,
    now: datetime | None = None,
) -> TapResult:
    with session_factory() as session:
        if isinstance(update, CallbackTap):
            result = handle_tap(session, settings, update, now=now)
        else:
            result = handle_text(session, update)
    perform(provider, result.actions)
    return result


# --- Sweep ------------------------------------------------------------------------------------


def close_finished_prompts(
    session_factory: sessionmaker[Session],
    provider: TextProvider,
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> int:
    """Remove the buttons from delivered prompts whose request was decided (by another
    approver, by voice, by the deadline) or whose own buttons expired."""
    now = now or utcnow()
    with session_factory() as session:
        candidates = session.execute(
            select(TextPrompt.id, TextPrompt.request_id)
            .join(ApprovalRequest, ApprovalRequest.id == TextPrompt.request_id)
            .where(
                TextPrompt.status.in_((PromptStatus.SENT, PromptStatus.CONFIRMING)),
                or_(
                    ApprovalRequest.status != RequestStatus.PENDING.value,
                    TextPrompt.expires_at <= now,
                    ApprovalRequest.expires_at <= now,
                ),
            )
            .limit(limit)
        ).all()
    closed = 0
    for prompt_id, request_id in candidates:
        with session_factory() as session, transaction(session):
            session.scalar(
                select(ApprovalRequest.id).where(ApprovalRequest.id == request_id).with_for_update()
            )
            prompt = session.scalar(
                select(TextPrompt).where(TextPrompt.id == prompt_id).with_for_update()
            )
            request = session.get(ApprovalRequest, request_id)
            if prompt is None or request is None or prompt.status not in PromptStatus.OPEN:
                continue
            reason = _closed_reason(request, prompt, now)
            if reason is None:
                continue
            note = "expired" if reason == "expired" else "closed"
            actions = _close_and_edit(session, prompt, reason, note)
        perform(provider, actions)
        closed += 1
    return closed
