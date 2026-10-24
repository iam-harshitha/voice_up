"""Centralised, validated application settings loaded from the environment."""

from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

REQUIRED_DIALECT = "postgresql+psycopg"


def _validate_database_url(value: str) -> str:
    try:
        url = make_url(value)
    except ArgumentError as exc:
        raise ValueError("must be a valid SQLAlchemy database URL") from exc
    if url.drivername != REQUIRED_DIALECT:
        raise ValueError(f"must use the '{REQUIRED_DIALECT}://' dialect")
    if not url.database:
        raise ValueError("must include a database name")
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    app_name: str = "Approval Line"
    api_v1_prefix: str = "/api/v1"
    database_url: str = Field(min_length=1)
    test_database_url: str | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    db_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)

    # --- Voice approvals (Chunk 2) ---------------------------------------------------
    # "disabled" turns voice off; "mock" is for tests and local development only and is
    # refused in production; "bolna" requires the credentials below.
    voice_provider: Literal["disabled", "mock", "bolna"] = "disabled"
    bolna_api_base_url: str = "https://api.bolna.ai"
    bolna_api_key: SecretStr | None = None
    bolna_agent_id: str | None = None
    bolna_from_phone_number: str | None = None
    # Names of the post-call extractions configured on the Bolna agent (Extractions tab).
    bolna_extraction_category: str = "Approval"
    bolna_decision_extraction: str = "Decision"
    bolna_identity_extraction: str = "Identity Confirmed"
    # Free-text extraction holding what a conditional approval requires (stored, never
    # interpreted).
    bolna_condition_extraction: str = "Condition"
    # Sent by Bolna as a custom webhook header; compared in constant time.
    voice_webhook_token: SecretStr | None = None
    voice_webhook_max_bytes: int = Field(default=256 * 1024, ge=1024, le=5 * 1024 * 1024)
    voice_min_decision_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    voice_http_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    voice_worker_poll_seconds: float = Field(default=2.0, gt=0, le=60)
    voice_job_lease_seconds: int = Field(default=120, ge=10, le=3600)
    voice_job_max_attempts: int = Field(default=5, ge=1, le=20)
    voice_retry_base_seconds: float = Field(default=5.0, gt=0, le=600)
    voice_retry_max_seconds: float = Field(default=300.0, gt=0, le=3600)
    # Poll the provider for calls with no terminal update after this long...
    voice_reconcile_after_seconds: int = Field(default=600, ge=30)
    # ...and give up on them (status timed_out) after this long.
    voice_call_timeout_seconds: int = Field(default=3600, ge=60)
    voice_max_call_generations: int = Field(default=3, ge=1, le=10)
    # Development only: directory where the mock provider keeps simulated executions so
    # the API, worker and CLI processes can share them.
    voice_mock_state_dir: str | None = None

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: str) -> str:
        return _validate_database_url(value)

    @field_validator("test_database_url")
    @classmethod
    def _check_test_database_url(cls, value: str | None) -> str | None:
        return _validate_database_url(value) if value else None

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("cors_origins")
    @classmethod
    def _reject_wildcard(cls, value: list[str]) -> list[str]:
        if "*" in value:
            raise ValueError("wildcard origins are not allowed; list explicit origins")
        return value

    @model_validator(mode="after")
    def _check_voice_configuration(self) -> Self:
        if self.voice_provider == "mock" and self.app_env == "production":
            raise ValueError("VOICE_PROVIDER=mock is not allowed when APP_ENV=production")
        if self.voice_provider == "bolna":
            missing = [
                name
                for name, value in (
                    ("BOLNA_API_KEY", self.bolna_api_key),
                    ("BOLNA_AGENT_ID", self.bolna_agent_id),
                    ("VOICE_WEBHOOK_TOKEN", self.voice_webhook_token),
                )
                if not value or (isinstance(value, SecretStr) and not value.get_secret_value())
            ]
            if missing:
                raise ValueError(f"VOICE_PROVIDER=bolna requires {', '.join(missing)}")
            if not self.bolna_api_base_url.startswith("https://"):
                raise ValueError("BOLNA_API_BASE_URL must use https")
        token = self.voice_webhook_token
        if (
            self.voice_provider != "disabled"
            and token is not None
            and len(token.get_secret_value()) < 32
        ):
            raise ValueError("VOICE_WEBHOOK_TOKEN must be at least 32 characters")
        # A job lease must outlive a provider request (connect + write + read timeouts),
        # or a second worker could take over a call whose first request is still in flight.
        if self.voice_job_lease_seconds < 4 * self.voice_http_timeout_seconds:
            raise ValueError(
                "VOICE_JOB_LEASE_SECONDS must be at least 4x VOICE_HTTP_TIMEOUT_SECONDS"
            )
        if self.voice_retry_max_seconds < self.voice_retry_base_seconds:
            raise ValueError("VOICE_RETRY_MAX_SECONDS must be >= VOICE_RETRY_BASE_SECONDS")
        if self.voice_call_timeout_seconds <= self.voice_reconcile_after_seconds:
            raise ValueError("VOICE_CALL_TIMEOUT_SECONDS must exceed VOICE_RECONCILE_AFTER_SECONDS")
        return self

    @field_validator("api_v1_prefix")
    @classmethod
    def _check_prefix(cls, value: str) -> str:
        if not value.startswith("/") or value.endswith("/"):
            raise ValueError("must start with '/' and must not end with '/'")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
