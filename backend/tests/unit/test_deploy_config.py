"""The production compose file pins APP_ENV=production, so the safeguards cannot be off.

Static checks only: Docker is not run here (see docs/deployment.md for `docker compose
config` and image-level validation).
"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.core.config import Settings

REPO = Path(__file__).resolve().parents[3]
COMPOSE = REPO / "deploy" / "docker-compose.prod.yml"
BACKEND_SERVICES = ("migrate", "api", "worker")
DB = "postgresql+psycopg://user:pass@localhost:5432/approval_line"


@pytest.fixture(scope="module")
def services() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


@pytest.mark.parametrize("name", BACKEND_SERVICES)
def test_backend_services_pin_production(services, name):
    service = services[name]
    # `environment` overrides `env_file`, so this value wins over the env file.
    assert service["environment"]["APP_ENV"] == "production"
    assert service["env_file"] == ".env.production"


@pytest.mark.parametrize("name", BACKEND_SERVICES)
def test_no_secrets_are_inlined_in_the_compose_file(services, name):
    # Only the non-secret APP_ENV is set inline; everything else comes from the env file.
    assert set(services[name]["environment"]) == {"APP_ENV"}


def test_the_pinned_environment_turns_the_safeguards_on(services):
    app_env = services["api"]["environment"]["APP_ENV"]
    base = {"_env_file": None, "database_url": DB, "app_env": app_env}
    for unsafe in (
        {"voice_provider": "mock"},
        {"dashboard_cookie_secure": False},
        {"cors_origins": "http://dashboard.example.com"},
    ):
        with pytest.raises(ValidationError):
            Settings(**base, **unsafe)
    assert Settings(**base).session_cookie_secure is True


def test_the_production_env_file_is_never_committed():
    ignore = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env.*" in ignore and "!.env.example" in ignore
