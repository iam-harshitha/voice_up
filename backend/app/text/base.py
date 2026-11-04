"""Provider-neutral types for text approvals. Only app/text/telegram.py knows Telegram's
field names; everything else uses these."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class ErrorKind(StrEnum):
    RETRYABLE = "retryable"  # network trouble, rate limit, provider 5xx
    PERMANENT = "permanent"  # bad token, chat not found, bot blocked, bad request


class TextProviderError(Exception):
    """A provider call failed. ``code`` is short and safe to store (never a URL, token or
    raw provider text)."""

    def __init__(self, kind: ErrorKind, code: str) -> None:
        super().__init__(code)
        self.kind = kind
        self.code = code[:64]


@dataclass(frozen=True)
class Button:
    label: str
    data: str  # callback data, at most 64 bytes


@dataclass(frozen=True)
class OutgoingMessage:
    chat_id: str
    text: str
    buttons: tuple[Button, ...] = ()


@dataclass(frozen=True)
class CallbackTap:
    """Someone tapped one of our inline buttons."""

    update_id: int
    callback_id: str
    sender_id: str
    chat_id: str | None
    message_id: str | None
    data: str


@dataclass(frozen=True)
class IncomingText:
    """A text message sent to the bot (a /start pairing code or anything else)."""

    update_id: int
    sender_id: str
    chat_id: str
    chat_type: str
    text: str


Update = CallbackTap | IncomingText


@dataclass(frozen=True)
class UpdateBatch:
    updates: list[Update] = field(default_factory=list)
    # Pass this as the next offset (confirms everything up to it); None = nothing new.
    next_offset: int | None = None


class TextProvider(Protocol):
    name: str

    def send_message(self, message: OutgoingMessage) -> str:
        """Send and return the provider's message ID."""
        ...

    def edit_message(
        self, chat_id: str, message_id: str, text: str, buttons: tuple[Button, ...]
    ) -> None: ...

    def answer_callback(self, callback_id: str, text: str) -> None: ...

    def get_updates(self, offset: int | None, timeout: int) -> UpdateBatch: ...

    def close(self) -> None: ...
