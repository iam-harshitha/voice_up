"""Deterministic approval-policy evaluation.

This module is deliberately free of FastAPI, SQLAlchemy and any provider code: it is the
single authority on whether a request is pending, approved or rejected. Callers (an API
client, a voice provider, an LLM) may submit votes, but never a decision.
"""

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

SUPPORTED_REQUIRED_APPROVALS = frozenset({1, 2})


class RequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class VoteChoice(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True)
class CastVote:
    approver_id: UUID
    choice: VoteChoice


class InvalidPolicyError(ValueError):
    """The policy parameters handed to the evaluator are outside the supported range."""


class IllegalTransitionError(Exception):
    """A vote was applied to a request that has already reached a final decision."""


def validate_required_approvals(required_approvals: int) -> None:
    # bool is an int subclass; True must not silently mean "one approval".
    if isinstance(required_approvals, bool) or not isinstance(required_approvals, int):
        raise InvalidPolicyError("required_approvals must be an integer")
    if required_approvals not in SUPPORTED_REQUIRED_APPROVALS:
        raise InvalidPolicyError("required_approvals must be 1 or 2")


def evaluate(
    required_approvals: int,
    votes: Iterable[CastVote],
    authorized_approver_ids: Collection[UUID],
) -> RequestStatus:
    """Compute the decision implied by the votes.

    - Votes from approvers outside ``authorized_approver_ids`` are ignored.
    - Any authorized reject vote rejects the request.
    - The request is approved once ``required_approvals`` distinct authorized approvers
      have approved; repeated approvals by one approver count once.
    - Anything else, including no votes at all, is pending. Missing votes, unanswered
      calls and timeouts are simply absent here and therefore never approve.
    """
    validate_required_approvals(required_approvals)
    authorized = set(authorized_approver_ids)
    if len(authorized) < required_approvals:
        raise InvalidPolicyError("fewer authorized approvers than required approvals")

    counted = [vote for vote in votes if vote.approver_id in authorized]
    if any(vote.choice is VoteChoice.REJECT for vote in counted):
        return RequestStatus.REJECTED
    approving = {vote.approver_id for vote in counted if vote.choice is VoteChoice.APPROVE}
    if len(approving) >= required_approvals:
        return RequestStatus.APPROVED
    return RequestStatus.PENDING


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
