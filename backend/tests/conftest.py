"""Shared fixtures.

Integration tests run against TEST_DATABASE_URL only. The schema of that database is
dropped and rebuilt with Alembic at the start of the session, so the suite refuses to run
unless the database name contains "test" and differs from DATABASE_URL.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.security import AGENT_SCOPES, ALL_SCOPES, Scope
from app.db.base import Base
from app.db.session import build_engine, build_session_factory, transaction
from app.main import create_app
from app.services import api_key_service
from tests.helpers import Tenant, create_approver, create_policy

BACKEND_DIR = Path(__file__).resolve().parents[1]


class _TestEnv(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")
    database_url: str | None = None
    test_database_url: str | None = None


def _safe_test_database_url() -> str | None:
    env = _TestEnv()
    if not env.test_database_url:
        return None
    test_url = make_url(env.test_database_url)
    if "test" not in (test_url.database or ""):
        pytest.exit("TEST_DATABASE_URL database name must contain 'test'", returncode=2)
    # Compare names only: the same name behind a different host alias is the same database.
    if env.database_url and make_url(env.database_url).database == test_url.database:
        pytest.exit("TEST_DATABASE_URL must not point at DATABASE_URL", returncode=2)
    return env.test_database_url


def alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.attributes["database_url"] = database_url
    config.attributes["configure_logger"] = False
    return config


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = _safe_test_database_url()
    if url is None:
        pytest.skip("TEST_DATABASE_URL is not set; PostgreSQL integration tests not run")
    return url


@pytest.fixture(scope="session")
def test_settings(test_database_url: str) -> Settings:
    return Settings(database_url=test_database_url, app_env="test", log_level="WARNING")


@pytest.fixture(scope="session")
def migrated_engine(test_settings: Settings) -> Iterator[Engine]:
    engine = build_engine(test_settings)
    dev_database = make_url(_TestEnv().database_url).database if _TestEnv().database_url else None
    with engine.begin() as connection:
        # Re-check against the database we are actually connected to (catches host
        # aliases such as localhost vs 127.0.0.1) before destroying anything.
        actual = connection.execute(text("SELECT current_database()")).scalar_one()
        if "test" not in actual or actual == dev_database:
            pytest.exit(f"refusing to reset non-test database {actual!r}", returncode=2)
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    command.upgrade(alembic_config(test_settings.database_url), "head")
    yield engine
    engine.dispose()


@pytest.fixture
def engine(migrated_engine: Engine) -> Iterator[Engine]:
    yield migrated_engine
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    with migrated_engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} CASCADE"))


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return build_session_factory(engine)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as db_session:
        yield db_session


@pytest.fixture
def client(test_settings: Settings, engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(test_settings)) as test_client:
        yield test_client


ADMIN_SCOPES = sorted(ALL_SCOPES - {Scope.VOTES_WRITE.value, Scope.REQUESTS_CREATE.value})


@pytest.fixture
def make_tenant(session_factory: sessionmaker[Session]) -> Callable[..., Tenant]:
    """Create an organization with admin, agent and voter keys (plus any extras)."""

    def factory(name: str = "Acme", **extra_keys: list[str]) -> Tenant:
        key_specs = {
            "admin": ADMIN_SCOPES,
            "agent": sorted(AGENT_SCOPES),
            "voter": [Scope.VOTES_WRITE.value],
            **extra_keys,
        }
        with session_factory() as db, transaction(db):
            organization = api_key_service.create_organization(db, name)
            tenant = Tenant(organization.id)
            for role, scopes in key_specs.items():
                issued = api_key_service.issue_api_key(db, organization.id, role, scopes)
                tenant.keys[role] = issued.plaintext
                tenant.key_ids[role] = issued.id
        return tenant

    return factory


@pytest.fixture
def tenant(make_tenant: Callable[..., Tenant]) -> Tenant:
    return make_tenant()


@pytest.fixture
def two_approver_setup(client: TestClient, tenant: Tenant) -> dict:
    alice = create_approver(client, tenant, "Alice", phone_number="+14155550101")
    bob = create_approver(client, tenant, "Bob")
    policy = create_policy(client, tenant, [alice["id"], bob["id"]])
    return {"alice": alice, "bob": bob, "policy": policy}
