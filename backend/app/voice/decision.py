"""Turn a provider execution into a vote candidate, or into an explicit non-decision.

This is a pure function. It never decides the request: it only says whether a call
produced a valid, explicit approve/reject from the expected person. The deterministic
policy engine (app/domain/approval_rules.py) still decides the request's status.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.domain.approval_rules import VoteChoice
from app.voice.base import CallStatus, DecisionChoice, ExtractedAnswer, ProviderExecution

_AFFIRMATIVE_IDENTITY = frozenset({"yes", "confirmed"})


class NoDecisionReason(StrEnum):
    CALL_NOT_COMPLETED = "call_not_completed"
    VOICEMAIL = "voicemail"
    CORRELATION_MISMATCH = "correlation_mismatch"
    EXTRACTION_MALFORMED = "extraction_malformed"
    IDENTITY_NOT_CONFIRMED = "identity_not_confirmed"
    DECISION_MISSING = "decision_missing"
    DECISION_UNRECOGNIZED = "decision_unrecognized"
    UNCERTAIN = "uncertain"
    NEEDS_CLARIFICATION = "needs_clarification"
    LOW_CONFIDENCE = "low_confidence"
    CONDITION_MISSING = "condition_missing"


@dataclass(frozen=True)
class ExpectedCall:
    """What the persisted call record says this execution must be."""

    provider_call_id: str
    internal_call_id: str
    agent_id: str
    recipient_phone_number: str | None
    min_confidence: float


@dataclass(frozen=True)
class Interpretation:
    candidate: DecisionChoice | None
    vote: VoteChoice | None
    reason: NoDecisionReason | None
    confidence: float | None
    # Set only for a valid conditional approval: what the approver required.
    condition: str | None = None

    @property
    def is_vote(self) -> bool:
        return self.vote is not None

    @property
    def is_condition(self) -> bool:
        return self.condition is not None


def _normalize(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().lower().replace("-", "_").replace(" ", "_") or None


def _confident(answer: ExtractedAnswer, minimum: float) -> bool:
    # A missing confidence is treated as insufficient rather than assumed high.
    return answer.confidence is not None and answer.confidence >= minimum


def _no(reason: NoDecisionReason, candidate=None, confidence=None) -> Interpretation:
    return Interpretation(candidate, None, reason, confidence)


def interpret(execution: ProviderExecution, expected: ExpectedCall) -> Interpretation:
    if execution.status is not CallStatus.COMPLETED:
        return _no(NoDecisionReason.CALL_NOT_COMPLETED)

    # The execution must be the exact call we placed, to the person we placed it to.
    # The echoed correlation ID is required: a missing one fails closed. The number is
    # checked only when present, because Bolna's PII masking nulls phone numbers.
    if (
        execution.provider_call_id != expected.provider_call_id
        or execution.agent_id != expected.agent_id
        or execution.correlation_id != expected.internal_call_id
        or (
            execution.to_number is not None
            and expected.recipient_phone_number is not None
            and execution.to_number != expected.recipient_phone_number
        )
    ):
        return _no(NoDecisionReason.CORRELATION_MISMATCH)

    if execution.answered_by_voicemail:
        return _no(NoDecisionReason.VOICEMAIL)
    if execution.extraction_malformed:
        return _no(NoDecisionReason.EXTRACTION_MALFORMED)

    identity = execution.identity
    if (
        identity is None
        or _normalize(identity.value) not in _AFFIRMATIVE_IDENTITY
        or not _confident(identity, expected.min_confidence)
    ):
        return _no(NoDecisionReason.IDENTITY_NOT_CONFIRMED)

    decision = execution.decision
    if decision is None or _normalize(decision.value) is None:
        return _no(NoDecisionReason.DECISION_MISSING)
    try:
        candidate = DecisionChoice(_normalize(decision.value))
    except ValueError:
        return _no(NoDecisionReason.DECISION_UNRECOGNIZED, confidence=decision.confidence)

    if candidate is DecisionChoice.UNCERTAIN:
        return _no(NoDecisionReason.UNCERTAIN, candidate, decision.confidence)
    if candidate is DecisionChoice.NEEDS_CLARIFICATION:
        return _no(NoDecisionReason.NEEDS_CLARIFICATION, candidate, decision.confidence)
    if not _confident(decision, expected.min_confidence):
        return _no(NoDecisionReason.LOW_CONFIDENCE, candidate, decision.confidence)
    if candidate is DecisionChoice.CONDITIONAL_APPROVE:
        # Never a vote. Without the stated condition there is nothing to record.
        if not execution.condition_text:
            return _no(NoDecisionReason.CONDITION_MISSING, candidate, decision.confidence)
        return Interpretation(candidate, None, None, decision.confidence, execution.condition_text)

    vote = VoteChoice.APPROVE if candidate is DecisionChoice.APPROVE else VoteChoice.REJECT
    return Interpretation(candidate, vote, None, decision.confidence)
