"""Centralised, validated application settings loaded from the environment."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
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

    @field_validator("api_v1_prefix")
    @classmethod
    def _check_prefix(cls, value: str) -> str:
        if not value.startswith("/") or value.endswith("/"):
            raise ValueError("must start with '/' and must not end with '/'")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
