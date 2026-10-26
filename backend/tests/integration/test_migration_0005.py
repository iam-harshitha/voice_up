"""Migration 0005 (request idempotency keys) is additive and reversible."""

import pytest
from alembic import command
from sqlalchemy import inspect, text

from tests.conftest import alembic_config
from tests.helpers import create_approver, create_request, make_policy

pytestmark = pytest.mark.integration


def columns(engine) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns("approval_requests")}


def test_round_trip_keeps_existing_requests(test_settings, engine, client, tenant):
    a = create_approver(client, tenant, "Alice")["id"]
    request = create_request(client, tenant, make_policy(client, tenant, [a])["id"])
    config = alembic_config(test_settings.database_url)
    command.downgrade(config, "0004")
    try:
        assert "idempotency_key_hash" not in columns(engine)
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM approval_requests WHERE id = :r"),
                    {"r": request["id"]},
                ).scalar()
                == 1
            )
    finally:
        command.upgrade(config, "head")
    assert {"idempotency_key_hash", "idempotency_fingerprint"} <= columns(engine)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT idempotency_key_hash, idempotency_fingerprint "
                "FROM approval_requests WHERE id = :r"
            ),
            {"r": request["id"]},
        ).one()
    assert tuple(row) == (None, None)  # pre-existing requests keep NULL (no key)
