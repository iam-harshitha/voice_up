import uuid

from app.db.models import ApprovalRequest, Approver
from app.voice.bolna import CORRELATION_VARIABLE
from app.voice.context import build_call_variables, select_details


def make_request(context: dict) -> ApprovalRequest:
    return ApprovalRequest(
        id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        action="deploy.production",
        summary="Deploy payments-api v2.3.0 to production",
        context=context,
        policy_snapshot={"required_approvals": 2, "approver_ids": []},
    )


def test_variables_describe_the_specific_request():
    approver = Approver(name="Alice", phone_number="+14155550101")
    variables = build_call_variables(
        make_request({"service": "payments-api", "version": "2.3.0", "risk": "high"}),
        approver,
        "call-123",
    )
    assert variables == {
        CORRELATION_VARIABLE: "call-123",
        "approver_name": "Alice",
        "request_reference": "12345678",
        "requested_action": "deploy production",
        "request_summary": "Deploy payments-api v2.3.0 to production",
        "request_details": "service: payments-api; version: 2.3.0",
        "risk_details": "high",
        "required_approvals": "2",
        "allowed_decisions": "approve or reject",
    }
    assert all(isinstance(value, str) for value in variables.values())
    assert "+14155550101" not in str(variables)


def test_sensitive_nested_and_oversized_context_is_not_read_out():
    details, risk = select_details(
        {
            "api_key": "sk-live-123",
            "db_password": "hunter2",
            "Authorization": "Bearer x",
            "customer_email": "a@example.com",
            "on_call_phone": "+14155550199",
            "nested": {"token": "abc"},
            "items": [1, 2, 3],
            "ticket": "CHG-1042",
            "notes": "x" * 500,
            "control": "line1\nline2\x00",
        }
    )
    for leaked in ("sk-live", "hunter2", "Bearer", "a@example.com", "+1415", "abc", "[1"):
        assert leaked not in details
    assert "ticket: CHG-1042" in details
    assert "control: line1 line2" in details
    notes = next(part for part in details.split("; ") if part.startswith("notes"))
    assert len(notes) <= len("notes: ") + 120
    assert risk == "not specified"


def test_detail_count_is_bounded():
    details, _ = select_details({f"k{i}": i for i in range(30)})
    assert details.count(";") == 7  # eight items


def guard_request(**context_overrides) -> ApprovalRequest:
    """What the Claude Code guard files: its summary repeats the action, command and folder."""
    return ApprovalRequest(
        id=uuid.UUID("3f2a9c1e-0000-4000-8000-000000000001"),
        action="deploy_to_production",
        summary="Claude Code wants to deploy to production by running: ./deploy.sh production "
        r"(in D:\work\demo-target).",
        context={
            "command": "./deploy.sh production",
            "working_directory": r"D:\work\demo-target",
            "guard_rule": "production deploy",
            "shell": "Bash",
            **context_overrides,
        },
        policy_snapshot={"required_approvals": 1, "approver_ids": []},
    )


def test_each_fact_is_said_once_for_a_guard_request():
    v = build_call_variables(guard_request(), Approver(name="Alice"), "call-1")
    assert v["requested_action"] == "deploy to production"
    assert v["request_summary"] == "run ./deploy.sh production in demo-target"
    # Details remain available, without the command and folder the summary states.
    assert v["request_details"] == "guard rule: production deploy; shell: Bash"
    assert v["risk_details"] == "not specified"
    assert "deploy.sh" not in v["request_details"] and "demo-target" not in v["request_details"]


def test_sanitisation_is_unchanged_for_the_new_facts():
    request = guard_request(api_key="sk-live-123", on_call_phone="+14155550199", risk="high")
    v = build_call_variables(request, Approver(name="Alice"), "call-1")
    for leaked in ("sk-live", "+1415"):
        assert leaked not in str(v)
    assert v["risk_details"] == "high"
    # A request without a command keeps its own summary, cleaned and bounded as before.
    plain = make_request({"ticket": "CHG-1"})
    assert build_call_variables(plain, Approver(name="A"), "c")["request_summary"] == (
        "Deploy payments-api v2.3.0 to production"
    )


def test_the_telegram_message_is_four_short_lines():
    from datetime import UTC, datetime

    from app.text.message import prompt_text

    text = prompt_text(
        guard_request(api_key="sk-live-123"),
        Approver(name="Alice"),
        datetime(2026, 11, 4, 10, 15, tzinfo=UTC),
    )
    assert text.split("\n") == [
        "Approve: deploy to production?",
        "Run: ./deploy.sh production (in demo-target)",
        "Risk: not stated",
        "Ref 3F2A9C1E, expires 10:15 UTC",
    ]
    assert "sk-live" not in text
