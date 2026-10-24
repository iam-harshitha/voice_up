from uuid import uuid4

import pytest

from app.domain.approval_rules import ApprovalMode, InvalidPolicyError, VetoMode
from app.domain.snapshot import build_snapshot, read_snapshot

A, B, C = uuid4(), uuid4(), uuid4()


def v2(**overrides):
    values = {
        "policy_id": uuid4(),
        "name": "Deploys",
        "version": 3,
        "mode": ApprovalMode.QUORUM,
        "required_approvals": 1,
        "veto_mode": VetoMode.DESIGNATED,
        "primaries": [(A, True), (B, False)],
        "backups": [C],
        "deadline_seconds": 3600,
        "escalation_after_seconds": 600,
        "max_escalations": 1,
    }
    values.update(overrides)
    return build_snapshot(**values)


def test_v1_snapshots_keep_chunk1_semantics():
    rules = read_snapshot(
        {
            "policy_id": str(uuid4()),
            "name": "x",
            "version": 1,
            "required_approvals": 2,
            "approver_ids": [str(A), str(B)],
        }
    )
    assert rules.rules.mode is ApprovalMode.UNANIMOUS
    assert rules.rules.veto_mode is VetoMode.ANY_REJECTION
    assert rules.primary_ids == (A, B)
    assert (rules.backups, rules.deadline_seconds, rules.max_escalations) == ((), None, 0)


def test_v2_round_trip():
    snapshot = v2()
    rules = read_snapshot(snapshot)
    assert snapshot["approver_ids"] == [str(A), str(B)]  # kept for older readers
    assert rules.rules.seats[0].can_veto and not rules.rules.seats[1].can_veto
    assert (rules.backups, rules.deadline_seconds, rules.escalation_after_seconds) == (
        (C,),
        3600,
        600,
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(schema_version=9),
        lambda s: s.update(veto_mode="sometimes"),
        lambda s: s.update(backups=[s["approver_ids"][0]]),
        lambda s: s.update(condition_rule="trust_me"),
        lambda s: s.update(approver_ids=[s["approver_ids"][1]]),
        lambda s: s.pop("primaries"),
        lambda s: s["primaries"].__setitem__(0, {"approver_id": "nope", "can_veto": True}),
    ],
)
def test_tampered_or_malformed_snapshots_are_refused(mutate):
    snapshot = v2()
    mutate(snapshot)
    with pytest.raises(InvalidPolicyError):
        read_snapshot(snapshot)


def test_unbuildable_snapshot_is_never_produced():
    with pytest.raises(InvalidPolicyError):
        v2(required_approvals=2, primaries=[(A, False)])
