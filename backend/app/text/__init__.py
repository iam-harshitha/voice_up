"""Text-message approvals: provider interface, Telegram adapter, mock, signed tokens."""

from app.core.config import Settings
from app.core.errors import ServiceUnavailableError
from app.text.base import TextProvider


def build_text_provider(settings: Settings) -> TextProvider:
    """Create the configured provider. Never falls back to the mock silently."""
    if settings.text_provider == "telegram":
        from app.text.telegram import TelegramProvider

        return TelegramProvider(settings)
    if settings.text_provider == "mock":
        if settings.app_env == "production":  # also enforced by Settings
            raise ServiceUnavailableError("Text is misconfigured.", code="text_unavailable")
        from app.text.mock import MockTextProvider

        return MockTextProvider(settings)
    raise ServiceUnavailableError("Text approvals are not enabled.", code="text_unavailable")


def bot_username(settings: Settings) -> str:
    """The bot behind pairing links (a fixed placeholder for the mock)."""
    if settings.text_provider == "mock":
        return settings.telegram_bot_username or "approval_line_mock_bot"
    return settings.telegram_bot_username or ""
