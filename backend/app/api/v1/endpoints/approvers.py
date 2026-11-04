from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from app.api.dependencies import DbSession, OrganizationContext, PageParams, require_scope
from app.core.errors import UnprocessableError
from app.core.security import Scope
from app.db.models import ApproverChannelLink
from app.schemas.common import Page
from app.schemas.policy import ApproverCreate, ApproverRead, ApproverUpdate
from app.schemas.text import (
    ChannelLinkCreate,
    ChannelLinkList,
    ChannelLinkRead,
    ChannelLinksRevoked,
    PairingLinkRead,
)
from app.services import channel_link_service, policy_service

router = APIRouter(prefix="/approvers", tags=["approvers"])

CanRead = Annotated[OrganizationContext, Depends(require_scope(Scope.APPROVERS_READ))]
CanWrite = Annotated[OrganizationContext, Depends(require_scope(Scope.APPROVERS_WRITE))]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_approver(body: ApproverCreate, org: CanWrite, session: DbSession) -> ApproverRead:
    approver = policy_service.create_approver(session, org.organization_id, body)
    return ApproverRead.model_validate(approver)


@router.get("")
def list_approvers(org: CanRead, session: DbSession, page: PageParams) -> Page[ApproverRead]:
    items, total = policy_service.list_approvers(
        session, org.organization_id, limit=page.limit, offset=page.offset
    )
    return Page[ApproverRead](
        items=[ApproverRead.model_validate(item) for item in items],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{approver_id}")
def get_approver(approver_id: UUID, org: CanRead, session: DbSession) -> ApproverRead:
    approver = policy_service.get_approver(session, org.organization_id, approver_id)
    return ApproverRead.model_validate(approver)


@router.patch("/{approver_id}")
def update_approver(
    approver_id: UUID, body: ApproverUpdate, org: CanWrite, session: DbSession
) -> ApproverRead:
    approver = policy_service.update_approver(session, org.organization_id, approver_id, body)
    return ApproverRead.model_validate(approver)


# --- Telegram channel links ---------------------------------------------------------------


def _link_read(link: ApproverChannelLink) -> ChannelLinkRead:
    read = ChannelLinkRead.model_validate(link)
    read.status = channel_link_service.effective_status(link)
    return read


@router.post("/{approver_id}/channel-links", status_code=status.HTTP_201_CREATED)
def create_channel_link(
    approver_id: UUID,
    body: ChannelLinkCreate,
    org: CanWrite,
    session: DbSession,
    http_request: Request,
) -> PairingLinkRead:
    """Issue a one-time Telegram pairing link (valid 10 minutes). Earlier unused links for
    the approver stop working; a verified link is replaced when the new one is used."""
    pairing = channel_link_service.start_pairing(
        session,
        http_request.app.state.settings,
        org.organization_id,
        approver_id,
        channel=body.channel,
        actor_api_key_id=org.principal.api_key_id,
    )
    return PairingLinkRead(**_link_read(pairing.link).model_dump(), pairing_url=pairing.url)


@router.get("/{approver_id}/channel-links")
def list_channel_links(approver_id: UUID, org: CanRead, session: DbSession) -> ChannelLinkList:
    links = channel_link_service.list_links(session, org.organization_id, approver_id)
    return ChannelLinkList(items=[_link_read(link) for link in links])


@router.delete("/{approver_id}/channel-links")
def revoke_channel_links(
    approver_id: UUID, org: CanWrite, session: DbSession, channel: str = "telegram"
) -> ChannelLinksRevoked:
    """Revoke the approver's pending and verified links; open prompts stop accepting taps."""
    if channel != "telegram":
        raise UnprocessableError("Unknown channel.", code="channel_invalid")
    revoked = channel_link_service.revoke_links(
        session,
        org.organization_id,
        approver_id,
        channel=channel,
        actor_api_key_id=org.principal.api_key_id,
    )
    return ChannelLinksRevoked(revoked=revoked)
