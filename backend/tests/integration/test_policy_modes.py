"""Policy configuration: validation, defaults, snapshots and policy audit events."""

import pytest
from sqlalchemy import select

from app.db.models import AuditEvent, RequestApprover
from tests.helpers import (
    API,
    create_approver,
    create_request,
    get_request,
    make_policy,
    post_policy,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def people(client, tenant):
    return [create_approver(client, tenant, f"P{i}")["id"] for i in range(5)]


def invalid(response, fragment):
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "approval_policy_invalid"
    assert fragment in response.json()["error"]["message"]


class TestDefaults:
    def test_defaults_reproduce_chunk1_behaviour(self, client, tenant, people):
        policy = make_policy(client, tenant, people[:2])
        assert (policy["approval_mode"], policy["veto_mode"]) == ("unanimous", "any_rejection")
        assert policy["backup_approvers"] == [] and policy["veto_approver_ids"] == []
        assert policy["decision_deadline_seconds"] is None
        assert (policy["escalation_after_seconds"], policy["max_escalations"]) == (None, 0)

    def test_unanimous_still_requires_exact_count(self, client, tenant, people):
        invalid(post_policy(client, tenant, people[:2], required_approvals=1), "exactly one")


class TestValidation:
    def test_quorum_two_of_three(self, client, tenant, people):
        policy = make_policy(
            client, tenant, people[:3], approval_mode="quorum", required_approvals=2
        )
        assert policy["required_approvals"] == 2 and len(policy["approvers"]) == 3

    @pytest.mark.parametrize(
        ("fields", "fragment"),
        [
            ({"approval_mode": "quorum", "required_approvals": 2, "count": 1}, "between 2 and 5"),
            ({"approval_mode": "quorum", "required_approvals": 1, "count": 6}, "between 1 and 5"),
            (
                {"approval_mode": "quorum", "required_approvals": 1, "veto_mode": "designated"},
                "veto holder",
            ),
            (
                {
                    "approval_mode": "quorum",
                    "required_approvals": 1,
                    "veto_mode": "none",
                    "veto": 1,
                },
                "designated",
            ),
            ({"veto_mode": "none"}, "any_rejection"),
            (
                {
                    "approval_mode": "quorum",
                    "required_approvals": 1,
                    "veto_mode": "designated",
                    "veto_outsider": True,
                },
                "Veto holders",
            ),
            ({"backups": 4}, "At most 3"),
            ({"escalation_after_seconds": 600}, "at least one backup"),
            ({"backups": 1, "max_escalations": 1}, "requires escalation_after_seconds"),
            (
                {"backups": 1, "escalation_after_seconds": 600, "max_escalations": 2},
                "between 1 and the number of backups",
            ),
            (
                {"backups": 1, "escalation_after_seconds": 600, "decision_deadline_seconds": 600},
                "before the decision deadline",
            ),
        ],
    )
    def test_invalid_configurations(self, client, tenant, fields, fragment):
        ids = [create_approver(client, tenant, f"X{i}")["id"] for i in range(10)]
        count = fields.pop("count", 2)
        body = dict(fields)
        body.setdefault("required_approvals", count)
        primaries = ids[:count]
        if fields.pop("veto", None):
            body.pop("veto", None)
            body["veto_approver_ids"] = primaries[:1]
        if body.pop("veto_outsider", False):
            body["veto_approver_ids"] = [ids[9]]
        backups = body.pop("backups", 0)
        if backups:
            body["backup_approver_ids"] = ids[6 : 6 + backups] if backups < 4 else ids[5:9]
        invalid(post_policy(client, tenant, primaries, **body), fragment)

    def test_backup_cannot_be_primary(self, client, tenant, people):
        invalid(
            post_policy(client, tenant, people[:2], backup_approver_ids=[people[0]]),
            "both a primary and a backup",
        )

    def test_backup_from_other_org_or_inactive_is_refused(
        self, client, tenant, people, make_tenant
    ):
        other = make_tenant("Globex")
        stranger = create_approver(client, other, "Stranger")["id"]
        invalid(
            post_policy(client, tenant, people[:1], backup_approver_ids=[stranger]), "not found"
        )
        client.patch(
            f"{API}/approvers/{people[4]}", json={"is_active": False}, headers=tenant.admin
        )
        invalid(
            post_policy(client, tenant, people[:1], backup_approver_ids=[people[4]]), "inactive"
        )

    @pytest.mark.parametrize(
        "fields",
        [
            {"decision_deadline_seconds": 30},
            {"decision_deadline_seconds": 31 * 24 * 3600},
            {"escalation_after_seconds": 10},
            {"max_escalations": 4},
            {"approval_mode": "majority"},
            {"veto_mode": "sometimes"},
            {"required_approvals": 3},
        ],
    )
    def test_out_of_range_values_are_schema_errors(self, client, tenant, people, fields):
        assert post_policy(client, tenant, people[:1], **fields).status_code == 422


class TestSnapshots:
    def test_request_snapshot_is_complete_and_normalised(self, client, tenant, people, session):
        policy = make_policy(
            client,
            tenant,
            people[:3],
            approval_mode="quorum",
            required_approvals=2,
            veto_mode="designated",
            veto_approver_ids=[people[0]],
            backup_approver_ids=[people[3], people[4]],
            decision_deadline_seconds=3600,
            escalation_after_seconds=600,
            max_escalations=2,
        )
        request = create_request(client, tenant, policy["id"])
        assert request["expires_at"] is not None
        assert request["evaluation"]["state"] == "awaiting_approvals"
        assert request["evaluation"]["authorized"] is False
        rows = session.scalars(select(RequestApprover).order_by(RequestApprover.position)).all()
        assert [(str(r.approver_id), r.role, r.can_veto) for r in rows] == [
            (people[0], "primary", True),
            (people[1], "primary", False),
            (people[2], "primary", False),
            (people[3], "backup", False),
            (people[4], "backup", False),
        ]

    def test_policy_edits_do_not_change_existing_requests(
        self, client, tenant, people, voice_tenant=None
    ):
        policy = make_policy(
            client, tenant, people[:3], approval_mode="quorum", required_approvals=2
        )
        request = create_request(client, tenant, policy["id"])
        response = client.patch(
            f"{API}/policies/{policy['id']}",
            json={"required_approvals": 1, "veto_mode": "none", "decision_deadline_seconds": 600},
            headers=tenant.admin,
        )
        assert response.status_code == 200 and response.json()["version"] == 2
        again = get_request(client, tenant, request["id"])
        assert again["required_approvals"] == 2 and again["expires_at"] is None
        assert again["policy_version"] == 1

    def test_approver_deactivation_does_not_alter_snapshot(self, client, tenant, people):
        policy = make_policy(client, tenant, people[:2])
        request = create_request(client, tenant, policy["id"])
        client.patch(
            f"{API}/approvers/{people[1]}", json={"is_active": False}, headers=tenant.admin
        )
        again = get_request(client, tenant, request["id"])
        assert again["authorized_approver_ids"] == people[:2]
        assert again["required_approvals"] == 2

    def test_caller_cannot_weaken_a_request(self, client, tenant, people):
        policy = make_policy(client, tenant, people[:2])
        for extra in (
            {"required_approvals": 1},
            {"veto_mode": "none"},
            {"expires_at": "2099-01-01T00:00:00Z"},
            {"approver_ids": people[:1]},
            {"approval_mode": "quorum"},
        ):
            response = client.post(
                f"{API}/requests",
                json={"policy_id": policy["id"], "action": "a", "summary": "s", **extra},
                headers=tenant.agent,
            )
            assert response.status_code == 422, extra


class TestPolicyAudit:
    def test_policy_changes_are_audited_with_the_actor(self, client, make_tenant, session):
        tenant = make_tenant(auditor=["audit:read"])
        a = create_approver(client, tenant, "A")["id"]
        b = create_approver(client, tenant, "B")["id"]
        policy = make_policy(client, tenant, [a])
        client.patch(
            f"{API}/policies/{policy['id']}", json={"name": "Renamed"}, headers=tenant.admin
        ).raise_for_status()
        client.patch(
            f"{API}/policies/{policy['id']}",
            json={"required_approvals": 2, "approver_ids": [a, b]},
            headers=tenant.admin,
        ).raise_for_status()
        timeline = client.get(
            f"{API}/policies/{policy['id']}/audit-events", headers=tenant.headers("auditor")
        ).json()["items"]
        assert [e["event_type"] for e in timeline] == [
            "policy.created",
            "policy.updated",
            "policy.updated",
        ]
        assert [e["event_data"]["version"] for e in timeline] == [1, 2, 3]
        assert timeline[1]["event_data"]["changed_fields"] == ["name"]
        assert timeline[2]["event_data"]["changed_fields"] == ["approver_ids", "required_approvals"]
        admin_key = str(tenant.key_ids["admin"])
        assert all(e["event_data"]["actor_api_key_id"] == admin_key for e in timeline)
        # A no-op update writes nothing.
        client.patch(
            f"{API}/policies/{policy['id']}", json={"name": "Renamed"}, headers=tenant.admin
        )
        session.expire_all()
        assert (
            len(session.scalars(select(AuditEvent).where(AuditEvent.policy_id.is_not(None))).all())
            == 3
        )

    def test_other_org_cannot_read_policy_audit(self, client, tenant, make_tenant, people):
        policy = make_policy(client, tenant, people[:1])
        other = make_tenant("Globex", auditor=["audit:read"])
        response = client.get(
            f"{API}/policies/{policy['id']}/audit-events", headers=other.headers("auditor")
        )
        assert response.status_code == 404
