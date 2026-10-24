"""Deterministic approval-policy evaluation.

This module is deliberately free of FastAPI, SQLAlchemy and any provider code: it is the
single authority on whether a request is pending, approved, rejected or expired. Callers
(an API client, a voice provider, an LLM) may submit votes, but never a decision.

Model (see docs/policies.md for the full semantics):

- A policy has **seats**, one per primary approver. A seat is decided by the first valid
  vote cast for it, by the primary or by a backup assigned to that seat by escalation.
  Backups replace a non-responsive primary; they never add seats or lower the quorum.
- ``unanimous``: every seat must approve (required_approvals == number of seats).
- ``quorum``: ``required_approvals`` seats must approve.
- Rejections: a seat rejection is a *veto* (immediate rejection) when the veto mode
  says so; otherwise it only removes that seat from the possible approvals. Whenever the
  approvals still possible fall below the quorum, the request is rejected.
- A condition blocks approval, even if the quorum is met: an open condition, and a
  condition resolved as satisfied whose approver has not yet re-approved
  unconditionally. Resolving a condition never approves anything by itself.
- Once the deadline has passed, a pending request can only expire; it can never approve.
- Missing votes, unanswered calls, timeouts and ambiguous answers are simply absent from
  the inputs, so they can never approve anything.
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

SUPPORTED_REQUIRED_APPROVALS = frozenset({1, 2})
MAX_PRIMARY_APPROVERS = 5
MAX_BACKUP_APPROVERS = 3


class RequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


TERMINAL_REQUEST_STATUSES = frozenset(
    {RequestStatus.APPROVED, RequestStatus.REJECTED, RequestStatus.EXPIRED}
)


class VoteChoice(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ApprovalMode(StrEnum):
    UNANIMOUS = "unanimous"
    QUORUM = "quorum"


class VetoMode(StrEnum):
    ANY_REJECTION = "any_rejection"  # every seat's rejection vetoes
    DESIGNATED = "designated"  # only seats flagged can_veto veto
    NONE = "none"  # no vetoes; rejections only count against the quorum


class EvaluationState(StrEnum):
    AWAITING_APPROVALS = "awaiting_approvals"
    WAITING_FOR_CONDITION = "waiting_for_condition"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ReasonCode(StrEnum):
    QUORUM_MET = "quorum_met"
    VETO = "veto"
    QUORUM_UNREACHABLE = "quorum_unreachable"
    DEADLINE_PASSED = "deadline_passed"
    CONDITION_OPEN = "condition_open"
    REAPPROVAL_REQUIRED = "reapproval_required"
    AWAITING_DECISIONS = "awaiting_decisions"


class NextAction(StrEnum):
    AWAIT_DECISIONS = "await_decisions"
    RESOLVE_CONDITION = "resolve_condition"
    NONE = "none"


class InvalidPolicyError(ValueError):
    """The policy parameters are outside the supported, deterministic range."""


class IllegalTransitionError(Exception):
    """A vote was applied to a request that has already reached a final decision."""


@dataclass(frozen=True)
class Seat:
    approver_id: UUID  # the primary approver who holds the seat
    can_veto: bool = False


@dataclass(frozen=True)
class PolicyRules:
    mode: ApprovalMode
    required_approvals: int
    veto_mode: VetoMode
    seats: tuple[Seat, ...]

    def seat(self, seat_id: UUID) -> Seat | None:
        return next((s for s in self.seats if s.approver_id == seat_id), None)

    def vetoes(self, seat: Seat) -> bool:
        if self.veto_mode is VetoMode.ANY_REJECTION:
            return True
        return self.veto_mode is VetoMode.DESIGNATED and seat.can_veto


@dataclass(frozen=True)
class Ballot:
    """A validated vote, already attributed to a seat by the service layer."""

    approver_id: UUID
    seat_id: UUID
    choice: VoteChoice


@dataclass(frozen=True)
class CastVote:
    """Chunk 1 input type: a vote whose seat is its own approver."""

    approver_id: UUID
    choice: VoteChoice


@dataclass(frozen=True)
class Evaluation:
    state: EvaluationState
    status: RequestStatus
    reason: ReasonCode
    approvals: int
    rejections: int
    required_approvals: int
    veto_triggered: bool
    outstanding_seats: tuple[UUID, ...] = field(default_factory=tuple)
    condition_seats: tuple[UUID, ...] = field(default_factory=tuple)
    reapproval_seats: tuple[UUID, ...] = field(default_factory=tuple)
    # Who must re-approve each of ``reapproval_seats`` (same order): the approver who
    # stated the condition, who may be a backup rather than the seat's primary.
    reapproval_approvers: tuple[UUID, ...] = field(default_factory=tuple)
    deadline_passed: bool = False

    @property
    def authorized(self) -> bool:
        return self.status is RequestStatus.APPROVED

    @property
    def next_action(self) -> NextAction:
        if self.state is EvaluationState.WAITING_FOR_CONDITION and self.condition_seats:
            return NextAction.RESOLVE_CONDITION
        if self.state in (
            EvaluationState.AWAITING_APPROVALS,
            EvaluationState.WAITING_FOR_CONDITION,  # waiting for a re-approval
        ):
            return NextAction.AWAIT_DECISIONS
        return NextAction.NONE


def validate_required_approvals(required_approvals: int) -> None:
    # bool is an int subclass; True must not silently mean "one approval".
    if isinstance(required_approvals, bool) or not isinstance(required_approvals, int):
        raise InvalidPolicyError("required_approvals must be an integer")
    if required_approvals not in SUPPORTED_REQUIRED_APPROVALS:
        raise InvalidPolicyError("required_approvals must be 1 or 2")


def validate_rules(rules: PolicyRules) -> None:
    validate_required_approvals(rules.required_approvals)
    seat_ids = [seat.approver_id for seat in rules.seats]
    if not seat_ids:
        raise InvalidPolicyError("a policy needs at least one primary approver")
    if len(seat_ids) > MAX_PRIMARY_APPROVERS:
        raise InvalidPolicyError(f"at most {MAX_PRIMARY_APPROVERS} primary approvers")
    if len(set(seat_ids)) != len(seat_ids):
        raise InvalidPolicyError("primary approvers must be distinct")
    if rules.required_approvals > len(seat_ids):
        raise InvalidPolicyError("required approvals exceed the number of primary approvers")
    if rules.mode is ApprovalMode.UNANIMOUS:
        if rules.required_approvals != len(seat_ids):
            raise InvalidPolicyError("unanimous policies require every primary approver")
        if rules.veto_mode is not VetoMode.ANY_REJECTION:
            raise InvalidPolicyError("unanimous policies use the any_rejection veto mode")
    veto_holders = [seat for seat in rules.seats if seat.can_veto]
    if rules.veto_mode is VetoMode.DESIGNATED and not veto_holders:
        raise InvalidPolicyError("designated veto mode needs at least one veto holder")
    if rules.veto_mode is not VetoMode.DESIGNATED and veto_holders:
        raise InvalidPolicyError("veto holders are only meaningful in designated veto mode")


def evaluate_request(
    rules: PolicyRules,
    ballots: Sequence[Ballot],
    condition_seats: Collection[UUID] = (),
    reapproval_seats: Collection[UUID] = (),
    *,
    reapproval_by: Mapping[UUID, UUID] | None = None,
    deadline_passed: bool = False,
    current: RequestStatus = RequestStatus.PENDING,
) -> Evaluation:
    """Evaluate a request. Pure and deterministic: same inputs, same output.

    ``ballots`` must be in recording order; per seat, the first ballot decides it.
    Ballots for seats not in the policy are ignored. ``condition_seats`` have an open
    condition; ``reapproval_seats`` have a satisfied condition whose approver has not
    re-approved. Both block approval. ``reapproval_by`` maps a reapproval seat to the
    approver who must re-approve it (default: the seat's primary). Precedence:
    veto > quorum unreachable > deadline passed > condition > quorum met > waiting.
    A request that is already final keeps its status.
    """
    validate_rules(rules)
    seat_ids = {seat.approver_id for seat in rules.seats}
    decided: dict[UUID, VoteChoice] = {}
    for ballot in ballots:
        if ballot.seat_id in seat_ids and ballot.seat_id not in decided:
            decided[ballot.seat_id] = ballot.choice

    approvals = sum(choice is VoteChoice.APPROVE for choice in decided.values())
    rejected = [seat_id for seat_id, choice in decided.items() if choice is VoteChoice.REJECT]
    veto = any(rules.vetoes(rules.seat(seat_id)) for seat_id in rejected)  # type: ignore[arg-type]
    still_possible = len(rules.seats) - len(rejected)
    # Conditions block approval whether or not their seat also holds a vote.
    open_conditions = tuple(s.approver_id for s in rules.seats if s.approver_id in condition_seats)
    awaiting_reapproval = tuple(
        s.approver_id
        for s in rules.seats
        if s.approver_id in reapproval_seats and s.approver_id not in open_conditions
    )
    blocked = set(open_conditions) | set(awaiting_reapproval)
    outstanding = tuple(
        seat.approver_id
        for seat in rules.seats
        if seat.approver_id not in decided and seat.approver_id not in blocked
    )

    def result(state: EvaluationState, status: RequestStatus, reason: ReasonCode) -> Evaluation:
        return Evaluation(
            state=state,
            status=status,
            reason=reason,
            approvals=approvals,
            rejections=len(rejected),
            required_approvals=rules.required_approvals,
            veto_triggered=veto,
            outstanding_seats=outstanding,
            condition_seats=open_conditions,
            reapproval_seats=awaiting_reapproval,
            reapproval_approvers=tuple(
                (reapproval_by or {}).get(seat_id, seat_id) for seat_id in awaiting_reapproval
            ),
            deadline_passed=deadline_passed,
        )

    if current is not RequestStatus.PENDING:
        # Final decisions are immutable; report them as they are.
        final_reason = {
            RequestStatus.APPROVED: ReasonCode.QUORUM_MET,
            RequestStatus.EXPIRED: ReasonCode.DEADLINE_PASSED,
            RequestStatus.REJECTED: ReasonCode.VETO if veto else ReasonCode.QUORUM_UNREACHABLE,
        }[current]
        return result(EvaluationState(current.value), current, final_reason)
    if veto:
        return result(EvaluationState.REJECTED, RequestStatus.REJECTED, ReasonCode.VETO)
    if still_possible < rules.required_approvals:
        return result(
            EvaluationState.REJECTED, RequestStatus.REJECTED, ReasonCode.QUORUM_UNREACHABLE
        )
    if deadline_passed:
        return result(EvaluationState.EXPIRED, RequestStatus.EXPIRED, ReasonCode.DEADLINE_PASSED)
    if open_conditions:
        return result(
            EvaluationState.WAITING_FOR_CONDITION, RequestStatus.PENDING, ReasonCode.CONDITION_OPEN
        )
    if awaiting_reapproval:
        return result(
            EvaluationState.WAITING_FOR_CONDITION,
            RequestStatus.PENDING,
            ReasonCode.REAPPROVAL_REQUIRED,
        )
    if approvals >= rules.required_approvals:
        return result(EvaluationState.APPROVED, RequestStatus.APPROVED, ReasonCode.QUORUM_MET)
    return result(
        EvaluationState.AWAITING_APPROVALS, RequestStatus.PENDING, ReasonCode.AWAITING_DECISIONS
    )


# --- Chunk 1 compatibility: the same engine with one seat per authorized approver --------


def _legacy_rules(required_approvals: int, authorized: Collection[UUID]) -> PolicyRules:
    validate_required_approvals(required_approvals)
    seats = tuple(Seat(approver_id) for approver_id in dict.fromkeys(authorized))
    if len(seats) < required_approvals:
        raise InvalidPolicyError("fewer authorized approvers than required approvals")
    mode = ApprovalMode.UNANIMOUS if len(seats) == required_approvals else ApprovalMode.QUORUM
    return PolicyRules(mode, required_approvals, VetoMode.ANY_REJECTION, seats)


def evaluate(
    required_approvals: int,
    votes: Iterable[CastVote],
    authorized_approver_ids: Collection[UUID],
) -> RequestStatus:
    """Unanimous/any-rejection evaluation over approver IDs (each approver is a seat)."""
    rules = _legacy_rules(required_approvals, authorized_approver_ids)
    ballots = [Ballot(v.approver_id, v.approver_id, v.choice) for v in votes]
    return evaluate_request(rules, ballots).status


def next_status(
    current: RequestStatus,
    required_approvals: int,
    votes: Iterable[CastVote],
    authorized_approver_ids: Collection[UUID],
) -> RequestStatus:
    """Apply the evaluator to a request, refusing to reopen a final decision."""
    if current is not RequestStatus.PENDING:
        raise IllegalTransitionError(f"request is already {current.value}")
    return evaluate(required_approvals, votes, authorized_approver_ids)
