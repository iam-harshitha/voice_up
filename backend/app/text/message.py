"""The approval message an approver reads in Telegram, and its buttons.

Four short lines built from the same sanitised facts the voice agent uses
(app/voice/context.py: secret-looking and personal keys dropped, values flattened and
bounded), each fact said once. Plain text only; Telegram is never asked to parse
formatting.
"""

from datetime import datetime

from app.db.models import ApprovalRequest, Approver
from app.text import tokens
from app.text.base import Button
from app.voice.context import request_facts


def prompt_text(request: ApprovalRequest, approver: Approver, expires_at: datetime) -> str:
    """Title, what will run, risk, reference. (The approver is the chat's owner, so their
    name is not repeated.)"""
    facts = request_facts(request)
    if facts.command is not None:
        run = f"Run: {facts.command}" + (f" (in {facts.folder})" if facts.folder else "")
    else:
        run = f"What: {facts.summary}"
    risk = facts.risk if facts.risk != "not specified" else "not stated"
    return "\n".join(
        [
            f"Approve: {facts.action}?",
            run,
            f"Risk: {risk}",
            f"Ref {facts.reference}, expires {expires_at:%H:%M} UTC",
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


# One line each.
CONFIRM_NOTE = "Tap Confirm approve to record it, or Cancel."
CANCELLED_NOTE = "Cancelled. Nothing was recorded."
FREE_TEXT_REPLY = "Please use the buttons. Text replies are never counted."
NOT_ACCEPTED = "Not accepted."

# Final line appended to the message once it stops accepting taps.
OUTCOME_NOTES = {
    "approve": "Approved by you.",
    "reject": "Rejected by you.",
    "closed": "Closed: no longer waiting for you.",
    "expired": "Expired: buttons no longer work.",
    "refused": "Not accepted: could not be recorded (see the audit trail).",
}
# Toast shown on the tapped button.
ANSWERS = {"approve": "Approved: recorded.", "reject": "Rejected: recorded."}


def with_note(text: str, note: str) -> str:
    return f"{text}\n{note}"
