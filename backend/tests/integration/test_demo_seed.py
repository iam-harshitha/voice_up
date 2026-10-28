"""demo-seed: additive, idempotent, never overwrites, never fabricates decisions."""

import argparse
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.cli import CliError, _demo_seed
from app.core.errors import NotFoundError
from app.db.models import (
    ApiKey,
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalVote,
    Approver,
    AuditEvent,
)
from app.services import demo_seed_service
from tests.helpers import API, create_approver, make_policy

pytestmark = pytest.mark.integration


def counts(session):
    session.expire_all()
    return {
        model.__tablename__: session.scalar(select(func.count()).select_from(model))
        for model in (Approver, ApprovalPolicy, ApprovalRequest, ApprovalVote)
    }


def test_seed_creates_the_demo_records_through_the_service_layer(session, tenant, client):
    result = demo_seed_service.seed(session, tenant.organization_id)
    assert set(result.approvers) == {"Demo Alice", "Demo Bob", "Demo Carol (backup)"}
    assert set(result.policies) == {"Demo: production deploys", "Demo: short deadline"}
    assert len(result.created) == 5 and result.reused == []
    policy = client.get(
        f"{API}/policies/{result.policies['Demo: production deploys']}", headers=tenant.admin
    ).json()
    assert (policy["approval_mode"], policy["required_approvals"]) == ("unanimous", 2)
    assert policy["decision_deadline_seconds"] == 3600
    assert [a["name"] for a in policy["approvers"]] == ["Demo Alice", "Demo Bob"]
    # Seeding never creates requests or votes: decisions only come from the workflow.
    assert counts(session)["approval_requests"] == 0
    assert counts(session)["approval_votes"] == 0
    # Policy creation went through the audited service path.
    created_events = session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.event_type == "policy.created", AuditEvent.policy_id == policy["id"])
    )
    assert created_events == 1


def test_seed_is_idempotent(session, tenant):
    first = demo_seed_service.seed(session, tenant.organization_id)
    before = counts(session)
    second = demo_seed_service.seed(session, tenant.organization_id)
    assert counts(session) == before
    assert second.approvers == first.approvers and second.policies == first.policies
    assert second.created == [] and len(second.reused) == 5


def test_seed_never_overwrites_a_conflicting_record(session, tenant, client):
    other = create_approver(client, tenant, "Someone")["id"]
    make_policy(client, tenant, [other], name="Demo: production deploys")
    with pytest.raises(demo_seed_service.DemoSeedError, match="different configuration"):
        demo_seed_service.seed(session, tenant.organization_id)
    policy = session.scalar(
        select(ApprovalPolicy).where(ApprovalPolicy.name == "Demo: production deploys")
    )
    assert policy.version == 1 and policy.required_approvals == 1


def test_seed_refuses_a_modified_demo_approver(session, tenant, client):
    create_approver(
        client, tenant, "Demo Alice", phone_number="+14155550199", email="alice.demo@example.com"
    )
    with pytest.raises(demo_seed_service.DemoSeedError, match="Demo Alice"):
        demo_seed_service.seed(session, tenant.organization_id)


def test_seed_requires_an_existing_organization(session):
    with pytest.raises(NotFoundError):
        demo_seed_service.seed(session, uuid.uuid4())


def args(org_id, **flags):
    return argparse.Namespace(
        org_id=org_id,
        yes=flags.get("yes", True),
        issue_agent_key=flags.get("issue_agent_key", False),
    )


def test_cli_requires_explicit_confirmation(session, tenant):
    with pytest.raises(CliError, match="--yes"):
        _demo_seed(
            args(tenant.organization_id, yes=False), SimpleNamespace(app_env="development"), session
        )
    assert counts(session)["approvers"] == 0


def test_cli_is_refused_in_production(session, tenant):
    with pytest.raises(CliError, match="production"):
        _demo_seed(args(tenant.organization_id), SimpleNamespace(app_env="production"), session)
    assert counts(session)["approvers"] == 0


def test_cli_issues_the_agent_key_once(session, tenant, capsys):
    dev = SimpleNamespace(app_env="development")
    _demo_seed(args(tenant.organization_id, issue_agent_key=True), dev, session)
    first = capsys.readouterr().out
    assert "key:" in first and "Demo: production deploys" in first
    assert "+1415555" not in first  # no contact details in the output
    _demo_seed(args(tenant.organization_id, issue_agent_key=True), dev, session)
    assert "not reissued" in capsys.readouterr().out
    session.expire_all()
    keys = session.scalars(select(ApiKey).where(ApiKey.name == "demo-agent")).all()
    assert len(keys) == 1
    assert sorted(keys[0].scopes) == [
        "audit:read",
        "policies:read",
        "requests:create",
        "requests:read",
    ]
