from uuid import uuid4

import pytest

from app.domain.approval_rules import (
    CastVote,
    InvalidPolicyError,
    RequestStatus,
    VoteChoice,
    evaluate,
)

A, B, OUTSIDER = uuid4(), uuid4(), uuid4()
APPROVE, REJECT = VoteChoice.APPROVE, VoteChoice.REJECT


def votes(*pairs):
    return [CastVote(approver_id, choice) for approver_id, choice in pairs]


@pytest.mark.parametrize(
    ("required", "authorized", "cast", "expected"),
    [
        (1, [A], votes((A, APPROVE)), RequestStatus.APPROVED),
        (1, [A], votes((A, REJECT)), RequestStatus.REJECTED),
        (1, [A], [], RequestStatus.PENDING),
        (2, [A, B], [], RequestStatus.PENDING),
        (2, [A, B], votes((A, APPROVE)), RequestStatus.PENDING),
        (2, [A, B], votes((A, APPROVE), (B, APPROVE)), RequestStatus.APPROVED),
        (2, [A, B], votes((A, APPROVE), (B, REJECT)), RequestStatus.REJECTED),
        (2, [A, B], votes((A, REJECT), (B, APPROVE)), RequestStatus.REJECTED),
        (2, [A, B], votes((B, REJECT)), RequestStatus.REJECTED),
    ],
)
def test_evaluate(required, authorized, cast, expected):
    assert evaluate(required, cast, authorized) is expected


def test_unauthorized_approve_does_not_count():
    result = evaluate(2, votes((A, APPROVE), (OUTSIDER, APPROVE)), [A, B])
    assert result is RequestStatus.PENDING


def test_unauthorized_reject_does_not_count():
    assert evaluate(1, votes((OUTSIDER, REJECT)), [A]) is RequestStatus.PENDING
    assert evaluate(1, votes((OUTSIDER, REJECT), (A, APPROVE)), [A]) is RequestStatus.APPROVED


def test_duplicate_approvals_from_one_approver_count_once():
    assert evaluate(2, votes((A, APPROVE), (A, APPROVE)), [A, B]) is RequestStatus.PENDING


def test_votes_from_any_iterable_are_accepted():
    assert evaluate(1, iter(votes((A, APPROVE))), {A}) is RequestStatus.APPROVED


@pytest.mark.parametrize("required", [0, 3, -1, True, 1.0, "2", None])
def test_invalid_required_approvals(required):
    with pytest.raises(InvalidPolicyError):
        evaluate(required, [], [A, B])


def test_fewer_authorized_approvers_than_required_is_invalid():
    with pytest.raises(InvalidPolicyError):
        evaluate(2, votes((A, APPROVE)), [A])
