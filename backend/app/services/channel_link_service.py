"""Linking approvers to their Telegram accounts (one-time pairing), listing and revoking.

Pairing: an operator issues a one-time code for an approver (only its SHA-256 is
stored; valid 10 minutes) and gives the approver the t.me/<bot>?start=<code> link. When
the approver presses Start, Telegram delivers "/start <code>" together with the sender's
user ID over our authenticated long poll, and complete_pairing() binds that account.

Rules: one verified link per approver per channel (re-pairing replaces the old link);
one approver per Telegram account (across all organizations: the bot is shared); a
code works once, only in a private chat, only before it expires.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BusinessRuleError, ServiceUnavailableError
from app.db.base import utcnow
from app.db.models import Approver, ApproverChannelLink
from app.db.models.text import Channel, LinkStatus
from app.db.session import transaction
from app.services import audit_service
from app.services.audit_service import AuditEventType
from app.services.policy_service import get_approver
from app.text import bot_username, tokens

logger = logging.getLogger(__name__)

PAIRING_TTL = timedelta(minutes=10)


@dataclass(frozen=True)
class PairingLink:
    link: ApproverChannelLink
    url: str  # contains the one-time code: shown once, never stored or logged


@dataclass(frozen=True)
class PairingResult:
    ok: bool
    reason: str
    reply: str


def effective_status(link: ApproverChannelLink, now: datetime | None = None) -> str:
    now = now or utcnow()
    if (
        link.status == LinkStatus.PENDING
        and link.pairing_expires_at is not None
        and link.pairing_expires_at <= now
    ):
        return "expired"
    return link.status


def _audit(session: Session, link: ApproverChannelLink, event: AuditEventType, **data) -> None:
    audit_service.record_event(
        session,
        organization_id=link.organization_id,
        approver_id=link.approver_id,
        event_type=event,
        data={"link_id": str(link.id), "channel": link.channel, **data},
    )


def _revoke(session: Session, link: ApproverChannelLink, now: datetime, reason: str, **data):
    was = link.status
    link.status = LinkStatus.REVOKED
    link.revoked_at = now
    if was == LinkStatus.VERIFIED:
        _audit(session, link, AuditEventType.CHANNEL_LINK_REVOKED, reason=reason, **data)


def start_pairing(
    session: Session,
    settings: Settings,
    organization_id: UUID,
    approver_id: UUID,
    *,
    channel: str = Channel.TELEGRAM,
    actor_api_key_id: UUID | None = None,
) -> PairingLink:
    """Issue a one-time pairing link. Earlier unused codes for the approver stop working."""
    if settings.text_provider == "disabled" or not bot_username(settings):
        raise ServiceUnavailableError(
            "Text approvals are not enabled (TEXT_PROVIDER).", code="text_unavailable"
        )
    now = utcnow()
    code = tokens.new_pairing_code()
    with transaction(session):
        approver = get_approver(session, organization_id, approver_id, for_update=True)
        if not approver.is_active:
            raise BusinessRuleError("The approver is inactive.", code="approver_inactive")
        for old in session.scalars(
            select(ApproverChannelLink).where(
                ApproverChannelLink.approver_id == approver_id,
                ApproverChannelLink.channel == channel,
                ApproverChannelLink.status == LinkStatus.PENDING,
            )
        ):
            _revoke(session, old, now, "superseded")
        link = ApproverChannelLink(
            organization_id=organization_id,
            approver_id=approver_id,
            channel=channel,
            status=LinkStatus.PENDING,
            pairing_code_hash=tokens.sha256_hex(code),
            pairing_expires_at=now + PAIRING_TTL,
            created_by_api_key_id=actor_api_key_id,
        )
        session.add(link)
        session.flush()
    logger.info("text.pairing_started", extra={"link_id": str(link.id)})
    return PairingLink(link, f"https://t.me/{bot_username(settings)}?start={code}")


def list_links(
    session: Session, organization_id: UUID, approver_id: UUID
) -> list[ApproverChannelLink]:
    get_approver(session, organization_id, approver_id)
    return list(
        session.scalars(
            select(ApproverChannelLink)
            .where(
                ApproverChannelLink.organization_id == organization_id,
                ApproverChannelLink.approver_id == approver_id,
            )
            .order_by(ApproverChannelLink.created_at.desc(), ApproverChannelLink.id)
        )
    )


def revoke_links(
    session: Session,
    organization_id: UUID,
    approver_id: UUID,
    *,
    channel: str = Channel.TELEGRAM,
    actor_api_key_id: UUID | None = None,
) -> int:
    """Revoke the approver's pending and verified links. Open prompts stop accepting taps
    (every tap re-checks the link)."""
    now = utcnow()
    with transaction(session):
        get_approver(session, organization_id, approver_id, for_update=True)
        links = list(
            session.scalars(
                select(ApproverChannelLink)
                .where(
                    ApproverChannelLink.organization_id == organization_id,
                    ApproverChannelLink.approver_id == approver_id,
                    ApproverChannelLink.channel == channel,
                    ApproverChannelLink.status.in_((LinkStatus.PENDING, LinkStatus.VERIFIED)),
                )
                .with_for_update()
            )
        )
        actor = str(actor_api_key_id) if actor_api_key_id else None
        for link in links:
            _revoke(session, link, now, "revoked", actor_api_key_id=actor)
    return len(links)


def verified_link(session: Session, approver_id: UUID, channel: str = Channel.TELEGRAM):
    return session.scalar(
        select(ApproverChannelLink).where(
            ApproverChannelLink.approver_id == approver_id,
            ApproverChannelLink.channel == channel,
            ApproverChannelLink.status == LinkStatus.VERIFIED,
        )
    )


_REPLIES = {
    "not_private_chat": "Open the pairing link in a private chat with this bot.",
    "invalid_code": "This pairing link is not valid. Ask for a new one.",
    "unknown_code": "This pairing link is not valid. Ask for a new one.",
    "code_revoked": "This pairing link is no longer valid. Ask for a new one.",
    "code_used": "This pairing link has already been used. Ask for a new one.",
    "code_expired": "This pairing link has expired. Ask for a new one.",
    "approver_inactive": "This approver is inactive, so the link was not made.",
    "account_already_linked": (
        "This Telegram account is already linked to another approver. Ask an operator "
        "to revoke that link first."
    ),
}


def _refuse(session: Session, link: ApproverChannelLink | None, reason: str) -> PairingResult:
    if link is not None:
        _audit(session, link, AuditEventType.CHANNEL_LINK_REJECTED, reason=reason)
    logger.info("text.pairing_refused", extra={"reason": reason})
    return PairingResult(False, reason, _REPLIES[reason])


def complete_pairing(
    session: Session,
    *,
    code: str,
    sender_id: str,
    chat_id: str,
    chat_type: str,
    now: datetime | None = None,
) -> PairingResult:
    """Bind the sender's Telegram account to the approver the code was issued for."""
    if chat_type != "private" or chat_id != sender_id:
        return PairingResult(False, "not_private_chat", _REPLIES["not_private_chat"])
    if not tokens.is_pairing_code(code):
        return PairingResult(False, "invalid_code", _REPLIES["invalid_code"])
    code_hash = tokens.sha256_hex(code)
    with transaction(session):
        link = session.scalar(
            select(ApproverChannelLink)
            .where(ApproverChannelLink.pairing_code_hash == code_hash)
            .with_for_update()
        )
        now = now or utcnow()
        if link is None:
            return _refuse(session, None, "unknown_code")
        if link.status == LinkStatus.VERIFIED:
            if link.external_user_id == sender_id:
                return PairingResult(True, "already_linked", "This chat is already linked.")
            return _refuse(session, link, "code_used")
        if link.status == LinkStatus.REVOKED:
            return _refuse(session, link, "code_revoked")
        if effective_status(link, now) == "expired":
            return _refuse(session, link, "code_expired")
        approver = session.scalar(
            select(Approver)
            .where(
                Approver.id == link.approver_id, Approver.organization_id == link.organization_id
            )
            .with_for_update(read=True)
        )
        if approver is None or not approver.is_active:
            return _refuse(session, link, "approver_inactive")
        taken = session.scalar(
            select(ApproverChannelLink.id).where(
                ApproverChannelLink.channel == link.channel,
                ApproverChannelLink.external_user_id == sender_id,
                ApproverChannelLink.status == LinkStatus.VERIFIED,
                ApproverChannelLink.approver_id != link.approver_id,
            )
        )
        if taken is not None:
            return _refuse(session, link, "account_already_linked")
        try:
            with session.begin_nested():
                previous = session.scalar(
                    select(ApproverChannelLink)
                    .where(
                        ApproverChannelLink.approver_id == link.approver_id,
                        ApproverChannelLink.channel == link.channel,
                        ApproverChannelLink.status == LinkStatus.VERIFIED,
                    )
                    .with_for_update()
                )
                if previous is not None:
                    _revoke(session, previous, now, "replaced", replaced_by=str(link.id))
                    session.flush()
                link.status = LinkStatus.VERIFIED
                link.external_user_id = sender_id
                link.external_chat_id = chat_id
                link.verified_at = now
                session.flush()
        except IntegrityError:
            # A concurrent pairing bound this account (or this approver) first.
            return _refuse(session, link, "account_already_linked")
        _audit(session, link, AuditEventType.CHANNEL_LINK_CREATED, replaced=previous is not None)
        name = approver.name
    logger.info("text.pairing_completed", extra={"link_id": str(link.id)})
    return PairingResult(True, "linked", f"Linked to {name}. Approval requests will arrive here.")
