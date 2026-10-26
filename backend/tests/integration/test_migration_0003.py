"""Migration 0003 against data written by the Chunk 1/2 schema (revision 0002)."""

import json
import uuid

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text

from tests.conftest import alembic_config

pytestmark = pytest.mark.integration


@pytest.fixture
def at_0002(test_settings, engine):
    config = alembic_config(test_settings.database_url)
    command.downgrade(config, "0002")
    yield config
    command.upgrade(config, "head")


def seed_chunk2_data(connection, *, at_head: bool = False):
    org, a, b, policy, request, vote = (uuid.uuid4() for _ in range(6))
    connection.execute(
        text("INSERT INTO organizations (id, name) VALUES (:o, 'Legacy')"), {"o": org}
    )
    for approver in (a, b):
        connection.execute(
            text(
                "INSERT INTO approvers (id, organization_id, name, email) "
                "VALUES (:i, :o, 'x', 'x@example.com')"
            ),
            {"i": approver, "o": org},
        )
    connection.execute(
        text(
            "INSERT INTO approval_policies (id, organization_id, name, "
            "required_approvals) VALUES (:p, :o, 'Old', 2)"
        ),
        {"p": policy, "o": org},
    )
    snapshot = json.dumps(
        {
            "policy_id": str(policy),
            "name": "Old",
            "version": 1,
            "required_approvals": 2,
            "approver_ids": [str(a), str(b)],
        }
    )
    connection.execute(
        text(
            "INSERT INTO approval_requests (id, organization_id, policy_id, "
            "policy_version, action, summary, policy_snapshot, status) VALUES "
            "(:r, :o, :p, 1, 'deploy', 'old request', CAST(:s AS jsonb), 'pending')"
        ),
        {"r": request, "o": org, "p": policy, "s": snapshot},
    )
    if at_head:  # head schema: snapshot membership rows and the vote's seat are required
        for position, approver in enumerate((a, b)):
            connection.execute(
                text(
                    "INSERT INTO request_approvers (request_id, approver_id, organization_id,"
                    " role, can_veto, position) VALUES (:r, :a, :o, 'primary', false, :p)"
                ),
                {"r": request, "a": approver, "o": org, "p": position},
            )
        connection.execute(
            text(
                "INSERT INTO approval_votes (id, organization_id, request_id, approver_id,"
                " seat_approver_id, vote) VALUES (:v, :o, :r, :a, :a, 'approve')"
            ),
            {"v": vote, "o": org, "r": request, "a": a},
        )
    else:
        connection.execute(
            text(
                "INSERT INTO approval_votes (id, organization_id, request_id, "
                "approver_id, vote) VALUES (:v, :o, :r, :a, 'approve')"
            ),
            {"v": vote, "o": org, "r": request, "a": a},
        )
    return {"org": org, "a": a, "b": b, "request": request}


def test_upgrade_backfills_existing_requests_and_votes(at_0002, engine):
    with engine.begin() as connection:
        ids = seed_chunk2_data(connection)
    command.upgrade(at_0002, "head")
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT approver_id, role, can_veto, position FROM request_approvers "
                "WHERE request_id = :r ORDER BY position"
            ),
            {"r": ids["request"]},
        ).all()
        assert [(r.approver_id, r.role, r.can_veto, r.position) for r in rows] == [
            (ids["a"], "primary", False, 0),
            (ids["b"], "primary", False, 1),
        ]
        seat = connection.execute(text("SELECT seat_approver_id FROM approval_votes")).scalar()
        assert seat == ids["a"]
        mode = connection.execute(
            text("SELECT approval_mode, veto_mode, max_escalations FROM approval_policies")
        ).one()
        assert tuple(mode) == ("unanimous", "any_rejection", 0)


def test_downgrade_refuses_when_chunk3_data_exists(test_settings, engine):
    config = alembic_config(test_settings.database_url)
    with engine.begin() as connection:
        ids = seed_chunk2_data(connection, at_head=True)
        connection.execute(
            text(
                "UPDATE approval_requests SET status = 'expired', decided_at = now() WHERE id = :r"
            ),
            {"r": ids["request"]},
        )
    with pytest.raises(Exception, match="refusing to downgrade 0003"):
        command.downgrade(config, "0002")
    # Still at head (the whole downgrade rolled back, including any later revisions it
    # had already stepped through), and the data is intact.
    head = ScriptDirectory.from_config(config).get_current_head()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == (
            head
        )
        assert (
            connection.execute(
                text("SELECT status FROM approval_requests WHERE id = :r"), {"r": ids["request"]}
            ).scalar()
            == "expired"
        )
