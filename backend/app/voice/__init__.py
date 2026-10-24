"""Voice approval calls: provider interface, Bolna adapter, mock, decision rules."""

from app.core.config import Settings
from app.core.errors import ServiceUnavailableError
from app.voice.base import VoiceProvider


def build_voice_provider(settings: Settings) -> VoiceProvider:
    """Create the configured provider. Never falls back to the mock silently."""
    if settings.voice_provider == "bolna":
        from app.voice.bolna import BolnaProvider

        return BolnaProvider(settings)
    if settings.voice_provider == "mock":
        if settings.app_env == "production":  # also enforced by Settings
            raise ServiceUnavailableError("Voice is misconfigured.", code="voice_unavailable")
        from app.voice.mock import MockVoiceProvider

        return MockVoiceProvider(settings)
    raise ServiceUnavailableError("Voice calls are not enabled.", code="voice_unavailable")


def provider_agent_id(provider: VoiceProvider, settings: Settings) -> str:
    """The agent ID every execution of ours must carry."""
    return getattr(provider, "agent_id", None) or settings.bolna_agent_id or ""
