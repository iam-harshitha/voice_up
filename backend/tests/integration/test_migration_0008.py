"""Migration 0008: voice fallback range 5-600 s; reversible while no policy uses < 10 s."""

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.conftest import alembic_config
from tests.helpers import create_approver, make_policy

pytestmark = pytest.mark.integration


def fallback(engine, policy_id: str) -> int:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT voice_fallback_after_seconds FROM approval_policies WHERE id = :p"),
            {"p": policy_id},
        ).scalar()


def test_round_trip_keeps_a_compatible_fallback(test_settings, engine, client, tenant):
    alice = create_approver(client, tenant, "Alice")["id"]
    policy = make_policy(client, tenant, [alice], voice_fallback_after_seconds=30)
    config = alembic_config(test_settings.database_url)
    command.downgrade(config, "0007")
    try:
        assert fallback(engine, policy["id"]) == 30
    finally:
        command.upgrade(config, "head")
    assert fallback(engine, policy["id"]) == 30


def test_downgrade_refuses_a_fallback_below_the_old_minimum(test_settings, engine, client, tenant):
    alice = create_approver(client, tenant, "Alice")["id"]
    make_policy(client, tenant, [alice], voice_fallback_after_seconds=5)
    config = alembic_config(test_settings.database_url)
    with pytest.raises(DBAPIError, match="refusing to downgrade 0008"):
        command.downgrade(config, "0007")
    command.upgrade(config, "head")


def test_the_database_enforces_5_to_600(engine, client, tenant):
    alice = create_approver(client, tenant, "Alice")["id"]
    policy = make_policy(client, tenant, [alice])
    for seconds in (4, 601):
        with pytest.raises(DBAPIError), engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE approval_policies SET voice_fallback_after_seconds = :s WHERE id = :p"
                ),
                {"s": seconds, "p": policy["id"]},
            )
