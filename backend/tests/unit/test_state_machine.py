from uuid import uuid4

import pytest

from app.domain.approval_rules import (
    CastVote,
    IllegalTransitionError,
    RequestStatus,
    VoteChoice,
    next_status,
)

A, B = uuid4(), uuid4()


def test_pending_with_insufficient_approvals_stays_pending():
    result = next_status(RequestStatus.PENDING, 2, [CastVote(A, VoteChoice.APPROVE)], [A, B])
    assert result is RequestStatus.PENDING


def test_pending_with_enough_approvals_becomes_approved():
    cast = [CastVote(A, VoteChoice.APPROVE), CastVote(B, VoteChoice.APPROVE)]
    assert next_status(RequestStatus.PENDING, 2, cast, [A, B]) is RequestStatus.APPROVED


def test_pending_with_reject_becomes_rejected():
    cast = [CastVote(A, VoteChoice.APPROVE), CastVote(B, VoteChoice.REJECT)]
    assert next_status(RequestStatus.PENDING, 2, cast, [A, B]) is RequestStatus.REJECTED


@pytest.mark.parametrize("final", [RequestStatus.APPROVED, RequestStatus.REJECTED])
@pytest.mark.parametrize("choice", list(VoteChoice))
def test_final_states_are_immutable(final, choice):
    with pytest.raises(IllegalTransitionError):
        next_status(final, 1, [CastVote(A, choice)], [A])


def test_no_votes_never_implies_approval():
    # Unanswered calls, timeouts and ambiguous answers produce no vote at all.
    for required, authorized in ((1, [A]), (2, [A, B])):
        assert next_status(RequestStatus.PENDING, required, [], authorized) is (
            RequestStatus.PENDING
        )
