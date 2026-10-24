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
        "requested_action": "deploy.production",
        "request_summary": "Deploy payments-api v2.3.0 to production",
        "request_details": "service: payments-api; version: 2.3.0",
        "risk_details": "risk: high",
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
