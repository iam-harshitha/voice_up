"""Provider-neutral voice types and the small interface every provider implements.

Business code only sees these types. Provider-specific field names stay inside the
adapter modules (``bolna.py``, ``mock.py``).
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class CallStatus(StrEnum):
    """Internal lifecycle of one intended approver call.

    queued -> initiating -> initiated -> ringing -> in_progress -> processing -> completed
    Terminal: completed, failed, timed_out, canceled, unknown.

    ``unknown`` means the provider may or may not have accepted the call (e.g. the HTTP
    request timed out after being sent). It is never retried automatically.
    """

    QUEUED = "queued"
    INITIATING = "initiating"
    INITIATED = "initiated"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


TERMINAL_STATUSES = frozenset(
    {
        CallStatus.COMPLETED,
        CallStatus.FAILED,
        CallStatus.TIMED_OUT,
        CallStatus.CANCELED,
        CallStatus.UNKNOWN,
    }
)

# Monotonic ordering of non-terminal states; a lower rank never overwrites a higher one.
_RANK = {
    CallStatus.QUEUED: 0,
    CallStatus.INITIATING: 1,
    CallStatus.INITIATED: 2,
    CallStatus.RINGING: 3,
    CallStatus.IN_PROGRESS: 4,
    CallStatus.PROCESSING: 5,
}


def can_transition(current: CallStatus, new: CallStatus) -> bool:
    """Lifecycle moves forward only, and terminal states are final."""
    if current in TERMINAL_STATUSES:
        return False
    if new in TERMINAL_STATUSES:
        return True
    return _RANK[new] > _RANK[current]


class DecisionChoice(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    UNCERTAIN = "uncertain"
    NEEDS_CLARIFICATION = "needs_clarification"


@dataclass(frozen=True)
class ExtractedAnswer:
    """One structured answer extracted from the conversation by the provider."""

    value: str | None
    confidence: float | None


@dataclass(frozen=True)
class ProviderExecution:
    """Normalised view of a provider call record (fetched or delivered)."""

    provider_call_id: str
    status: CallStatus
    provider_status: str
    agent_id: str | None = None
    to_number: str | None = None
    answered_by_voicemail: bool = False
    # Our internal voice-call ID echoed back from the per-call variables, if present.
    correlation_id: str | None = None
    decision: ExtractedAnswer | None = None
    identity: ExtractedAnswer | None = None
    # True when the extraction block existed but could not be parsed.
    extraction_malformed: bool = False


@dataclass(frozen=True)
class CallRequest:
    internal_call_id: str
    recipient_phone_number: str
    variables: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class InitiatedCall:
    provider_call_id: str
    provider_status: str


class ErrorKind(StrEnum):
    # The request certainly did not create a call; safe to retry later.
    RETRYABLE = "retryable"
    # The provider rejected the request (bad input, credentials, configuration).
    PERMANENT = "permanent"
    # The provider may have created a call; retrying could call the person twice.
    AMBIGUOUS = "ambiguous"
    # The provider has no such call.
    NOT_FOUND = "not_found"


class VoiceProviderError(Exception):
    """A provider failure carrying a safe category, never raw provider text or secrets."""

    def __init__(self, kind: ErrorKind, category: str) -> None:
        super().__init__(f"{kind.value}:{category}")
        self.kind = kind
        self.category = category


class VoiceProvider(Protocol):
    name: str

    def initiate_call(self, call: CallRequest) -> InitiatedCall: ...

    def get_execution(self, provider_call_id: str) -> ProviderExecution: ...
