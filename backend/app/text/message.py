"""The approval message an approver reads in Telegram, and its buttons.

It shows exactly what the voice agent reads out: the sanitised variables built by
app/voice/context.py (secret-looking and personal keys dropped, values flattened and
bounded). Plain text only; Telegram is never asked to parse formatting.
"""

from datetime import datetime

from app.db.models import ApprovalRequest, Approver
from app.text import tokens
from app.text.base import Button
from app.voice.context import build_call_variables


def prompt_text(request: ApprovalRequest, approver: Approver, expires_at: datetime) -> str:
    v = build_call_variables(request, approver, internal_call_id="")
    return "\n".join(
        [
            f"Approval request {v['request_reference']}",
            f"For: {v['approver_name']}",
            "",
            f"Action: {v['requested_action']}",
            f"Summary: {v['request_summary']}",
            f"Details: {v['request_details']}",
            f"Risk: {v['risk_details']}",
            f"Approvals required: {v['required_approvals']}",
            "",
            f"Buttons expire at {expires_at:%H:%M} UTC. Text replies are never counted.",
        ]
    )


def first_stage_buttons(secret: str, nonce: str) -> tuple[Button, ...]:
    return (
        Button("Approve", tokens.sign(secret, tokens.APPROVE, nonce)),
        Button("Reject", tokens.sign(secret, tokens.REJECT, nonce)),
    )


def confirm_buttons(secret: str, nonce: str) -> tuple[Button, ...]:
    return (
        Button("Confirm approve", tokens.sign(secret, tokens.CONFIRM, nonce)),
        Button("Cancel", tokens.sign(secret, tokens.CANCEL, nonce)),
    )


CONFIRM_NOTE = "You tapped Approve. Tap 'Confirm approve' to record it, or Cancel."
FREE_TEXT_REPLY = (
    "Please use the buttons on an approval message. Text replies are never counted as decisions."
)
NOT_ACCEPTED = "Not accepted."

# Final notes appended to the message once it stops accepting taps.
OUTCOME_NOTES = {
    "approve": "Recorded: you approved this request.",
    "reject": "Recorded: you rejected this request.",
    "closed": "Closed: this request is no longer waiting for your decision.",
    "expired": "Expired: these buttons no longer work.",
    "refused": "Not accepted: the decision could not be recorded (see the request's audit).",
}


def with_note(text: str, note: str) -> str:
    return f"{text}\n\n{note}"
