"""The read-only demo doctor: verdicts, and that it never prints secrets or writes anything.

Runs against the test database with HTTP (API, tunnel, Bolna) behind a mock transport.
"""

import importlib.util
import json
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, text

from app.db.base import utcnow
from app.db.models import AuditEvent, VoiceJob
from approval_line_agents.guard import ENV_RULES
from tests.helpers import create_approver, create_request, make_policy

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "demo_doctor", BACKEND / "scripts" / "demo_doctor.py"
)
doctor = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = doctor  # dataclasses resolve their module by name
_spec.loader.exec_module(doctor)

TUNNEL = "https://demo-tunnel.trycloudflare.test"
WEBHOOK_TOKEN = "doctor-webhook-token-" + "w" * 32
BOLNA_KEY = "bn-doctor-test-key-never-printed"
PHONE = "+14155550191"


def bolna_settings(test_settings):
    return test_settings.model_copy(
        update={
            "voice_provider": "bolna",
            "bolna_api_key": SecretStr(BOLNA_KEY),
            "bolna_agent_id": "agent-under-test",
            "voice_webhook_token": SecretStr(WEBHOOK_TOKEN),
        }
    )


class Network:
    """API, tunnel and Bolna as one mock transport. Records every request."""

    def __init__(self, *, bolna_url=TUNNEL + doctor.WEBHOOK_PATH, bolna_token=WEBHOOK_TOKEN):
        self.seen: list[str] = []
        self.bolna_url = bolna_url
        self.bolna_token = bolna_token

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(f"{request.method} {request.url}")
        path = request.url.path
        if path.endswith("/health/ready"):
            return httpx.Response(200, json={"status": "ok", "database": "ok"})
        if path.endswith("/api/v1/requests"):
            return httpx.Response(200, json={"items": [], "total": 0, "limit": 1, "offset": 0})
        if path == doctor.WEBHOOK_PATH:
            assert "X-Approval-Line-Webhook-Token" not in request.headers
            return httpx.Response(401, json={"error": {"code": "webhook_unauthorized"}})
        if path.startswith("/v2/agent/"):
            return httpx.Response(
                200,
                json={
                    "webhook_url": self.bolna_url,
                    "webhook_headers": {"X-Approval-Line-Webhook-Token": self.bolna_token},
                },
            )
        return httpx.Response(404)


@pytest.fixture
def world(client, make_tenant, test_settings, tmp_path, monkeypatch):
    # Never read the developer's real .demo/tunnel-url.txt.
    monkeypatch.setattr(doctor, "TUNNEL_URL_FILE", tmp_path / "no-tunnel-url.txt")
    tenant = make_tenant(guard=["requests:create", "requests:read", "policies:read"])
    approver = create_approver(client, tenant, "Alice", phone_number=PHONE)
    policy = make_policy(client, tenant, [approver["id"]], name="Deploys", auto_dial=True)
    rules = tmp_path / "rules.json"

    def write_rules(*policy_ids):
        rules.write_text(
            json.dumps(
                {
                    "rules": [
                        {"pattern": f"x{i}", "policy_id": p, "action": "a", "summary": "s"}
                        for i, p in enumerate(policy_ids)
                    ]
                }
            ),
            encoding="utf-8",
        )

    write_rules(policy["id"])
    env = {
        "APPROVAL_LINE_API_URL": "http://127.0.0.1:8000",
        "APPROVAL_LINE_API_KEY": tenant.keys["guard"],
        ENV_RULES: str(rules),
        "APPROVAL_LINE_PUBLIC_URL": TUNNEL,
    }

    def run(*, network=None, workers=1, version="2.1.300 (Claude Code)", **env_overrides):
        network = network or Network()
        with httpx.Client(transport=httpx.MockTransport(network.handle)) as http:
            report = doctor.run_doctor(
                {**env, **env_overrides},
                http=http,
                claude_output=lambda: version,
                workers=lambda: workers,
                settings=bolna_settings(test_settings),
            )
        output = doctor.render(report)
        for secret in (tenant.keys["guard"], WEBHOOK_TOKEN, BOLNA_KEY, PHONE):
            assert secret not in output
        return report, output, network

    return {
        "run": run,
        "tenant": tenant,
        "client": client,
        "policy": policy,
        "approver": approver,
        "write_rules": write_rules,
    }


def status_of(report, name_prefix):
    return [c.status for c in report.checks if c.name.startswith(name_prefix)]


def test_a_ready_setup_passes(world, session):
    events_before = session.scalar(select(func.count()).select_from(AuditEvent))
    report, output, network = world["run"]()
    assert report.ready, output
    assert "Verdict: READY" in output
    assert status_of(report, "Policy ") == ["PASS"]
    assert status_of(report, "Agent key scopes") == ["PASS"]
    assert status_of(report, "Bolna webhook URL") == ["PASS"]
    assert status_of(report, "Bolna webhook token") == ["PASS"]
    # Read-only: GETs plus one token-less webhook POST that is refused.
    assert all(s.startswith("GET ") for s in network.seen if doctor.WEBHOOK_PATH not in s)
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == events_before


def test_old_claude_code_fails(world):
    report, output, _ = world["run"](version="2.1.109 (Claude Code)")
    assert not report.ready and "fail-open" in output


def test_policy_with_auto_dial_off_fails(world):
    world["client"].patch(
        f"/api/v1/policies/{world['policy']['id']}",
        json={"auto_dial": False},
        headers=world["tenant"].admin,
    )
    report, output, _ = world["run"]()
    assert not report.ready and "auto-dial is OFF" in output


def test_policy_without_a_callable_approver_fails(world):
    tenant, client = world["tenant"], world["client"]
    no_phone = create_approver(client, tenant, "Bob")  # e-mail only
    other = make_policy(client, tenant, [no_phone["id"]], name="NoPhone", auto_dial=True)
    world["write_rules"](world["policy"]["id"], other["id"])
    report, output, _ = world["run"]()
    assert not report.ready
    assert status_of(report, "Policy ").count("FAIL") == 1
    assert "without a phone number" in output


def test_unknown_policy_and_other_organizations_fail(world, make_tenant, client):
    other_tenant = make_tenant()
    approver = create_approver(client, other_tenant, "Eve", phone_number="+14155550192")
    foreign = make_policy(client, other_tenant, [approver["id"]], name="Foreign", auto_dial=True)
    world["write_rules"](foreign["id"], "00000000-0000-4000-8000-000000000000")
    report, output, _ = world["run"]()
    assert "different organization" in output and "does not exist" in output
    assert not report.ready


def test_agent_key_missing_scopes_or_unknown_fails(world, make_tenant):
    weak = make_tenant(weak=["requests:read"])
    report, output, _ = world["run"](APPROVAL_LINE_API_KEY=weak.keys["weak"])
    assert not report.ready and "missing ['requests:create']" in output
    report, output, _ = world["run"](APPROVAL_LINE_API_KEY="al_not_a_real_key_" + "z" * 30)
    assert not report.ready and "not found in this database" in output


def test_worker_missing_or_stuck_fails(world, session_factory, client):
    report, output, _ = world["run"](workers=0)
    assert not report.ready and "no app.workers.voice_worker process" in output

    # A pending job long overdue: a worker process exists but is not draining the queue.
    policy = make_policy(
        client,
        world["tenant"],
        [world["approver"]["id"]],
        name="Deadline",
        decision_deadline_seconds=3600,
    )
    create_request(client, world["tenant"], policy["id"])
    with session_factory() as db:
        db.execute(
            text("UPDATE voice_jobs SET run_after = :t WHERE status = 'pending'"),
            {"t": utcnow() - timedelta(minutes=10)},
        )
        db.commit()
        assert db.scalar(select(func.count()).select_from(VoiceJob)) >= 1
    report, output, _ = world["run"](workers=1)
    assert not report.ready and "overdue" in output


def test_bolna_webhook_mismatches_fail(world):
    report, output, _ = world["run"](network=Network(bolna_url="https://old.trycloudflare.test/x"))
    assert not report.ready and "Bolna has https://old.trycloudflare.test/x" in output
    report, output, _ = world["run"](network=Network(bolna_token="stale-token"))
    assert not report.ready and "does not match VOICE_WEBHOOK_TOKEN" in output


def test_missing_rules_and_tunnel_fail(world):
    report, output, _ = world["run"](**{ENV_RULES: "", "APPROVAL_LINE_PUBLIC_URL": ""})
    assert not report.ready
    assert "APPROVAL_LINE_GUARD_RULES is not set" in output
    assert status_of(report, "Public tunnel") == ["FAIL"]
    assert status_of(report, "Bolna webhook URL") == ["WARN"]  # nothing to compare with
