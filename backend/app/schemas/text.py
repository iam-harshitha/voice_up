from datetime import datetime
from typing import Literal
from uuid import UUID

from app.schemas.common import ApiOutput, StrictInput


class ChannelLinkCreate(StrictInput):
    channel: Literal["telegram"] = "telegram"


class ChannelLinkRead(ApiOutput):
    """A link's state. Never includes the Telegram user or chat ID, or any code."""

    id: UUID
    channel: str
    # pending | verified | revoked | expired (a pending code past its 10 minutes)
    status: str
    created_at: datetime
    pairing_expires_at: datetime | None
    verified_at: datetime | None
    revoked_at: datetime | None


class ChannelLinkList(ApiOutput):
    items: list[ChannelLinkRead]


class PairingLinkRead(ChannelLinkRead):
    # One-time: open it on the approver's phone and press Start. Shown only here.
    pairing_url: str


class ChannelLinksRevoked(ApiOutput):
    revoked: int
