"""Migration 0007 (text approvals) is additive and reversible, and refuses to downgrade
while text data exists (a text-only policy would silently fall back to phone calls)."""

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from tests.conftest import alembic_config
from tests.helpers import create_approver, make_policy

pytestmark = pytest.mark.integration

TEXT_TABLES = {"approver_channel_links", "text_prompts"}


def shape(engine) -> tuple[bool, set[str], bool]:
    inspector = inspect(engine)
    policy_columns = {c["name"] for c in inspector.get_columns("approval_policies")}
    audit_columns = {c["name"] for c in inspector.get_columns("audit_events")}
    return (
        set(inspector.get_table_names()) >= TEXT_TABLES,
        policy_columns & {"approval_channel", "voice_fallback_after_seconds"},
        "approver_id" in audit_columns,
    )


def test_round_trip_keeps_existing_policies_on_voice(test_settings, engine, client, tenant):
    alice = create_approver(client, tenant, "Alice")["id"]
    policy = make_policy(client, tenant, [alice], auto_dial=True)
    config = alembic_config(test_settings.database_url)
    command.downgrade(config, "0006")
    try:
        assert shape(engine) == (False, set(), False)
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT auto_dial FROM approval_policies WHERE id = :p"), {"p": policy["id"]}
            ).scalar()
        assert row is True  # untouched
    finally:
        command.upgrade(config, "head")
    assert shape(engine) == (True, {"approval_channel", "voice_fallback_after_seconds"}, True)
    with engine.connect() as connection:
        channel, fallback = connection.execute(
            text(
                "SELECT approval_channel, voice_fallback_after_seconds "
                "FROM approval_policies WHERE id = :p"
            ),
            {"p": policy["id"]},
        ).one()
    assert (channel, fallback) == ("voice", 60)  # re-added with the safe defaults


def test_downgrade_refuses_while_a_text_policy_exists(test_settings, engine, client, tenant):
    alice = create_approver(client, tenant, "Alice")["id"]
    make_policy(client, tenant, [alice], approval_channel="text")
    config = alembic_config(test_settings.database_url)
    with pytest.raises(DBAPIError, match="refusing to downgrade 0007"):
        command.downgrade(config, "0006")
    assert shape(engine)[0] is True  # nothing was dropped
    command.upgrade(config, "head")  # still at head: a no-op


def test_the_database_enforces_the_new_checks(engine, client, tenant):
    alice = create_approver(client, tenant, "Alice")["id"]
    policy = make_policy(client, tenant, [alice])
    for statement in (
        "UPDATE approval_policies SET approval_channel = 'sms' WHERE id = :p",
        "UPDATE approval_policies SET voice_fallback_after_seconds = 4 WHERE id = :p",
    ):
        with pytest.raises(DBAPIError), engine.begin() as connection:
            connection.execute(text(statement), {"p": policy["id"]})
    with pytest.raises(DBAPIError, match="has_subject"), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO audit_events (id, organization_id, event_type, event_data) "
                "VALUES (gen_random_uuid(), :o, 'x', '{}'::jsonb)"
            ),
            {"o": str(tenant.organization_id)},
        )
