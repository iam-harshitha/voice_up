"""Migration 0006 (policy auto_dial) is additive and reversible; existing policies stay off."""

import pytest
from alembic import command
from sqlalchemy import inspect, text

from tests.conftest import alembic_config
from tests.helpers import create_approver, make_policy

pytestmark = pytest.mark.integration


def columns(engine) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns("approval_policies")}


def test_round_trip_keeps_existing_policies_off(test_settings, engine, client, tenant):
    a = create_approver(client, tenant, "Alice")["id"]
    policy = make_policy(client, tenant, [a], auto_dial=True)
    config = alembic_config(test_settings.database_url)
    command.downgrade(config, "0005")
    try:
        assert "auto_dial" not in columns(engine)
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM approval_policies WHERE id = :p"),
                    {"p": policy["id"]},
                ).scalar()
                == 1
            )
    finally:
        command.upgrade(config, "head")
    assert "auto_dial" in columns(engine)
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT auto_dial FROM approval_policies WHERE id = :p"), {"p": policy["id"]}
        ).scalar()
    assert value is False  # re-added with the safe default: never silently on
