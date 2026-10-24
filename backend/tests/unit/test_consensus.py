"""Consensus engine: quorum, veto modes, conditions, deadline precedence, determinism."""

import itertools
from uuid import uuid4

import pytest

from app.domain.approval_rules import (
    ApprovalMode,
    Ballot,
    EvaluationState,
    InvalidPolicyError,
    NextAction,
    PolicyRules,
    ReasonCode,
    RequestStatus,
    Seat,
    VetoMode,
    VoteChoice,
    evaluate_request,
    validate_rules,
)

A, B, C, BACKUP, OUTSIDER = (uuid4() for _ in range(5))
APPROVE, REJECT = VoteChoice.APPROVE, VoteChoice.REJECT


def rules(mode=ApprovalMode.QUORUM, required=2, veto=VetoMode.ANY_REJECTION, vetoers=()):
    seats = tuple(Seat(s, s in vetoers) for s in (A, B, C))
    if mode is ApprovalMode.UNANIMOUS:
        seats = seats[:required]
    return PolicyRules(mode, required, veto, seats)


def ballot(approver, choice, seat=None):
    return Ballot(approver, seat or approver, choice)


class TestQuorum:
    def test_two_of_three_met(self):
        result = evaluate_request(rules(), [ballot(A, APPROVE), ballot(C, APPROVE)])
        assert (result.status, result.reason) == (RequestStatus.APPROVED, ReasonCode.QUORUM_MET)
        assert result.authorized and result.outstanding_seats == (B,)

    def test_one_approval_never_satisfies_two(self):
        result = evaluate_request(rules(), [ballot(A, APPROVE)])
        assert result.status is RequestStatus.PENDING and not result.authorized
        assert result.state is EvaluationState.AWAITING_APPROVALS
        assert result.outstanding_seats == (B, C)
        assert result.next_action is NextAction.AWAIT_DECISIONS

    def test_duplicate_ballots_for_one_seat_count_once(self):
        result = evaluate_request(rules(), [ballot(A, APPROVE), ballot(A, APPROVE)])
        assert result.approvals == 1 and result.status is RequestStatus.PENDING

    def test_ballots_for_unknown_seats_are_ignored(self):
        result = evaluate_request(
            rules(veto=VetoMode.NONE), [ballot(OUTSIDER, APPROVE), ballot(A, APPROVE)]
        )
        assert result.approvals == 1 and result.status is RequestStatus.PENDING

    def test_first_ballot_per_seat_wins(self):
        # A backup decided A's seat first; a later conflicting ballot for the seat is moot.
        result = evaluate_request(
            rules(veto=VetoMode.NONE),
            [ballot(BACKUP, APPROVE, seat=A), ballot(A, REJECT), ballot(B, APPROVE)],
        )
        assert result.status is RequestStatus.APPROVED and result.rejections == 0


class TestVeto:
    def test_any_rejection_vetoes(self):
        result = evaluate_request(rules(), [ballot(A, APPROVE), ballot(B, REJECT)])
        assert (result.status, result.reason) == (RequestStatus.REJECTED, ReasonCode.VETO)
        assert result.veto_triggered

    def test_designated_veto_holder_rejects(self):
        r = rules(veto=VetoMode.DESIGNATED, vetoers=(A,))
        result = evaluate_request(r, [ballot(B, APPROVE), ballot(C, APPROVE), ballot(A, REJECT)])
        # B and C approved first, but the request was already decided by then in real
        # life; here all ballots are present and the veto still wins over quorum.
        assert (result.status, result.reason) == (RequestStatus.REJECTED, ReasonCode.VETO)

    def test_non_veto_rejection_only_counts_against_quorum(self):
        r = rules(veto=VetoMode.DESIGNATED, vetoers=(A,))
        result = evaluate_request(r, [ballot(B, REJECT)])
        assert result.status is RequestStatus.PENDING and not result.veto_triggered
        result = evaluate_request(r, [ballot(B, REJECT), ballot(A, APPROVE), ballot(C, APPROVE)])
        assert result.status is RequestStatus.APPROVED

    def test_no_veto_mode_rejects_only_when_quorum_unreachable(self):
        r = rules(veto=VetoMode.NONE)
        assert evaluate_request(r, [ballot(A, REJECT)]).status is RequestStatus.PENDING
        result = evaluate_request(r, [ballot(A, REJECT), ballot(B, REJECT)])
        assert (result.status, result.reason) == (
            RequestStatus.REJECTED,
            ReasonCode.QUORUM_UNREACHABLE,
        )
        assert not result.veto_triggered

    def test_backup_inherits_its_seats_veto(self):
        r = rules(veto=VetoMode.DESIGNATED, vetoers=(A,))
        result = evaluate_request(r, [ballot(BACKUP, REJECT, seat=A)])
        assert result.reason is ReasonCode.VETO

    def test_unanimous_any_rejection_rejects(self):
        r = rules(ApprovalMode.UNANIMOUS, 2)
        result = evaluate_request(r, [ballot(A, APPROVE), ballot(B, REJECT)])
        assert result.status is RequestStatus.REJECTED


class TestConditionsAndDeadline:
    def test_open_condition_blocks_approval_even_with_quorum(self):
        result = evaluate_request(
            rules(), [ballot(A, APPROVE), ballot(B, APPROVE)], condition_seats={C}
        )
        assert result.status is RequestStatus.PENDING and not result.authorized
        assert (result.state, result.reason) == (
            EvaluationState.WAITING_FOR_CONDITION,
            ReasonCode.CONDITION_OPEN,
        )
        assert result.next_action is NextAction.RESOLVE_CONDITION
        assert result.condition_seats == (C,) and result.outstanding_seats == ()

    def test_satisfied_condition_still_blocks_until_reapproval(self):
        result = evaluate_request(
            rules(), [ballot(A, APPROVE), ballot(B, APPROVE)], reapproval_seats={C}
        )
        assert result.status is RequestStatus.PENDING
        assert result.reason is ReasonCode.REAPPROVAL_REQUIRED
        assert result.next_action is NextAction.AWAIT_DECISIONS

    def test_reapproval_names_who_must_act(self):
        # A backup holding seat C stated the condition: the backup must re-approve.
        result = evaluate_request(
            rules(),
            [ballot(A, APPROVE), ballot(B, APPROVE)],
            reapproval_seats={C},
            reapproval_by={C: BACKUP},
        )
        assert (result.reapproval_seats, result.reapproval_approvers) == ((C,), (BACKUP,))
        default = evaluate_request(rules(), [ballot(A, APPROVE)], reapproval_seats={C})
        assert default.reapproval_approvers == (C,)  # the seat's primary by default

    def test_condition_does_not_prevent_rejection(self):
        result = evaluate_request(rules(), [ballot(A, REJECT)], condition_seats={B})
        assert result.status is RequestStatus.REJECTED

    def test_deadline_beats_approval_for_pending_requests(self):
        result = evaluate_request(
            rules(), [ballot(A, APPROVE), ballot(B, APPROVE)], deadline_passed=True
        )
        assert (result.status, result.reason) == (
            RequestStatus.EXPIRED,
            ReasonCode.DEADLINE_PASSED,
        )
        assert not result.authorized

    def test_veto_beats_deadline(self):
        result = evaluate_request(rules(), [ballot(A, REJECT)], deadline_passed=True)
        assert result.status is RequestStatus.REJECTED

    def test_no_votes_after_deadline_expires_never_approves(self):
        result = evaluate_request(rules(required=1), [], deadline_passed=True)
        assert result.status is RequestStatus.EXPIRED and not result.authorized

    @pytest.mark.parametrize(
        "final", [RequestStatus.APPROVED, RequestStatus.REJECTED, RequestStatus.EXPIRED]
    )
    def test_final_decisions_are_reported_unchanged(self, final):
        result = evaluate_request(rules(), [ballot(A, REJECT)], current=final)
        assert result.status is final and result.next_action is NextAction.NONE


def test_evaluation_is_deterministic_over_input_order_of_distinct_seats():
    ballots = [ballot(A, APPROVE), ballot(B, REJECT), ballot(C, APPROVE)]
    outcomes = {
        evaluate_request(rules(veto=VetoMode.NONE), list(order))
        for order in itertools.permutations(ballots)
    }
    assert len(outcomes) == 1
    assert evaluate_request(rules(veto=VetoMode.NONE), ballots) == evaluate_request(
        rules(veto=VetoMode.NONE), ballots
    )


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        (PolicyRules(ApprovalMode.QUORUM, 2, VetoMode.NONE, (Seat(A),)), "exceed"),
        (
            PolicyRules(ApprovalMode.UNANIMOUS, 1, VetoMode.ANY_REJECTION, (Seat(A), Seat(B))),
            "every primary",
        ),
        (PolicyRules(ApprovalMode.UNANIMOUS, 1, VetoMode.NONE, (Seat(A),)), "any_rejection"),
        (
            PolicyRules(ApprovalMode.QUORUM, 1, VetoMode.DESIGNATED, (Seat(A), Seat(B))),
            "veto holder",
        ),
        (
            PolicyRules(ApprovalMode.QUORUM, 1, VetoMode.NONE, (Seat(A, True), Seat(B))),
            "designated",
        ),
        (PolicyRules(ApprovalMode.QUORUM, 1, VetoMode.NONE, (Seat(A), Seat(A))), "distinct"),
        (PolicyRules(ApprovalMode.QUORUM, 3, VetoMode.NONE, (Seat(A), Seat(B), Seat(C))), "1 or 2"),
        (PolicyRules(ApprovalMode.QUORUM, 1, VetoMode.NONE, ()), "at least one"),
        (
            PolicyRules(
                ApprovalMode.QUORUM, 1, VetoMode.NONE, tuple(Seat(uuid4()) for _ in range(6))
            ),
            "at most",
        ),
    ],
)
def test_invalid_or_contradictory_rules_are_rejected(policy, message):
    with pytest.raises(InvalidPolicyError, match=message):
        validate_rules(policy)
