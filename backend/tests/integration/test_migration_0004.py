"""Migration 0004 (dashboard users and sessions) upgrades and downgrades cleanly."""

import pytest
from alembic import command
from sqlalchemy import inspect

from tests.conftest import alembic_config
from tests.helpers import create_dashboard_user

pytestmark = pytest.mark.integration


def tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def test_round_trip_keeps_approval_history_tables(test_settings, engine, session_factory, tenant):
    create_dashboard_user(session_factory, tenant.organization_id)
    config = alembic_config(test_settings.database_url)
    command.downgrade(config, "0003")
    try:
        present = tables(engine)
        assert "dashboard_users" not in present and "dashboard_sessions" not in present
        # Approval data is untouched by the downgrade.
        assert {"approval_requests", "audit_events", "api_keys", "organizations"} <= present
    finally:
        command.upgrade(config, "head")
    assert {"dashboard_users", "dashboard_sessions"} <= tables(engine)
