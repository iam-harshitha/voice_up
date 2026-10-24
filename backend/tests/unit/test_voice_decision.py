from dataclasses import replace

import pytest

from app.domain.approval_rules import VoteChoice
from app.voice.base import CallStatus, ExtractedAnswer, ProviderExecution, can_transition
from app.voice.decision import ExpectedCall, NoDecisionReason, interpret

EXPECTED = ExpectedCall(
    provider_call_id="exec-1",
    internal_call_id="call-1",
    agent_id="agent-1",
    recipient_phone_number="+14155550101",
    min_confidence=0.8,
)


def execution(**overrides) -> ProviderExecution:
    base = ProviderExecution(
        provider_call_id="exec-1",
        status=CallStatus.COMPLETED,
        provider_status="completed",
        agent_id="agent-1",
        to_number="+14155550101",
        correlation_id="call-1",
        decision=ExtractedAnswer("approve", 0.95),
        identity=ExtractedAnswer("yes", 0.95),
    )
    return replace(base, **overrides)


@pytest.mark.parametrize(
    ("value", "vote"),
    [
        ("approve", VoteChoice.APPROVE),
        ("reject", VoteChoice.REJECT),
        ("  Approve ", VoteChoice.APPROVE),
        ("REJECT", VoteChoice.REJECT),
    ],
)
def test_explicit_confident_decision_is_a_vote(value, vote):
    result = interpret(execution(decision=ExtractedAnswer(value, 0.9)), EXPECTED)
    assert result.vote is vote and result.reason is None


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"status": CallStatus.FAILED}, NoDecisionReason.CALL_NOT_COMPLETED),
        ({"status": CallStatus.PROCESSING}, NoDecisionReason.CALL_NOT_COMPLETED),
        ({"answered_by_voicemail": True}, NoDecisionReason.VOICEMAIL),
        ({"extraction_malformed": True}, NoDecisionReason.EXTRACTION_MALFORMED),
        ({"decision": None}, NoDecisionReason.DECISION_MISSING),
        ({"decision": ExtractedAnswer(None, 0.9)}, NoDecisionReason.DECISION_MISSING),
        ({"decision": ExtractedAnswer("", 0.9)}, NoDecisionReason.DECISION_MISSING),
        ({"decision": ExtractedAnswer("yes", 0.9)}, NoDecisionReason.DECISION_UNRECOGNIZED),
        (
            {"decision": ExtractedAnswer("approve with conditions", 0.99)},
            NoDecisionReason.DECISION_UNRECOGNIZED,
        ),
        ({"decision": ExtractedAnswer("uncertain", 0.99)}, NoDecisionReason.UNCERTAIN),
        (
            {"decision": ExtractedAnswer("needs_clarification", 0.99)},
            NoDecisionReason.NEEDS_CLARIFICATION,
        ),
        (
            {"decision": ExtractedAnswer("needs clarification", 0.99)},
            NoDecisionReason.NEEDS_CLARIFICATION,
        ),
        ({"decision": ExtractedAnswer("approve", 0.79)}, NoDecisionReason.LOW_CONFIDENCE),
        ({"decision": ExtractedAnswer("approve", None)}, NoDecisionReason.LOW_CONFIDENCE),
        ({"identity": None}, NoDecisionReason.IDENTITY_NOT_CONFIRMED),
        ({"identity": ExtractedAnswer("no", 0.99)}, NoDecisionReason.IDENTITY_NOT_CONFIRMED),
        ({"identity": ExtractedAnswer("yes", 0.5)}, NoDecisionReason.IDENTITY_NOT_CONFIRMED),
        ({"provider_call_id": "exec-2"}, NoDecisionReason.CORRELATION_MISMATCH),
        ({"agent_id": "other-agent"}, NoDecisionReason.CORRELATION_MISMATCH),
        ({"agent_id": None}, NoDecisionReason.CORRELATION_MISMATCH),
        ({"correlation_id": "call-2"}, NoDecisionReason.CORRELATION_MISMATCH),
        ({"to_number": "+14155550199"}, NoDecisionReason.CORRELATION_MISMATCH),
    ],
)
def test_anything_less_than_explicit_never_votes(overrides, reason):
    result = interpret(execution(**overrides), EXPECTED)
    assert result.vote is None
    assert result.reason is reason


def test_reject_with_low_confidence_is_not_a_vote_either():
    result = interpret(execution(decision=ExtractedAnswer("reject", 0.3)), EXPECTED)
    assert result.vote is None and result.reason is NoDecisionReason.LOW_CONFIDENCE


def test_masked_number_and_missing_correlation_are_tolerated():
    # PII masking nulls phone numbers; the provider-call-ID match still binds the call.
    result = interpret(execution(to_number=None, correlation_id=None), EXPECTED)
    assert result.vote is VoteChoice.APPROVE


@pytest.mark.parametrize(
    ("current", "new", "allowed"),
    [
        (CallStatus.QUEUED, CallStatus.INITIATING, True),
        (CallStatus.INITIATED, CallStatus.IN_PROGRESS, True),
        (CallStatus.IN_PROGRESS, CallStatus.RINGING, False),  # late ringing event
        (CallStatus.PROCESSING, CallStatus.IN_PROGRESS, False),
        (CallStatus.IN_PROGRESS, CallStatus.IN_PROGRESS, False),
        (CallStatus.RINGING, CallStatus.COMPLETED, True),
        (CallStatus.COMPLETED, CallStatus.FAILED, False),
        (CallStatus.FAILED, CallStatus.COMPLETED, False),
        (CallStatus.UNKNOWN, CallStatus.INITIATED, False),
        (CallStatus.TIMED_OUT, CallStatus.COMPLETED, False),
    ],
)
def test_lifecycle_is_monotonic_and_terminal_states_are_final(current, new, allowed):
    assert can_transition(current, new) is allowed
