"""The immutable rules a request was created under, and how to read them back.

Version 1 (Chunk 1/2 requests): {"policy_id", "name", "version", "required_approvals",
"approver_ids"}; semantics: unanimous, any rejection vetoes, no backups or deadline.

Version 2 adds explicit "approval_mode", "veto_mode", "primaries" (approver_id, can_veto),
"backups" (escalation order), "deadline_seconds", "escalation_after_seconds",
"max_escalations" and "condition_rule". "approver_ids" (the primaries) is kept so
existing readers keep working.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.domain.approval_rules import (
    ApprovalMode,
    InvalidPolicyError,
    PolicyRules,
    Seat,
    VetoMode,
    validate_rules,
)

SNAPSHOT_VERSION = 2
# A satisfied condition never approves by itself: the approver who set it must approve
# again, unconditionally. This is the only supported rule.
CONDITION_RULE_REAPPROVAL = "reapproval_required"


@dataclass(frozen=True)
class RequestRules:
    rules: PolicyRules
    backups: tuple[UUID, ...]
    deadline_seconds: int | None
    escalation_after_seconds: int | None
    max_escalations: int

    @property
    def primary_ids(self) -> tuple[UUID, ...]:
        return tuple(seat.approver_id for seat in self.rules.seats)


def build_snapshot(
    *,
    policy_id: UUID,
    name: str,
    version: int,
    mode: ApprovalMode,
    required_approvals: int,
    veto_mode: VetoMode,
    primaries: list[tuple[UUID, bool]],
    backups: list[UUID],
    deadline_seconds: int | None,
    escalation_after_seconds: int | None,
    max_escalations: int,
) -> dict[str, Any]:
    snapshot = {
        "schema_version": SNAPSHOT_VERSION,
        "policy_id": str(policy_id),
        "name": name,
        "version": version,
        "approval_mode": mode.value,
        "required_approvals": required_approvals,
        "veto_mode": veto_mode.value,
        "approver_ids": [str(approver_id) for approver_id, _ in primaries],
        "primaries": [
            {"approver_id": str(approver_id), "can_veto": can_veto}
            for approver_id, can_veto in primaries
        ],
        "backups": [str(approver_id) for approver_id in backups],
        "deadline_seconds": deadline_seconds,
        "escalation_after_seconds": escalation_after_seconds,
        "max_escalations": max_escalations,
        "condition_rule": CONDITION_RULE_REAPPROVAL,
    }
    read_snapshot(snapshot)  # never persist a snapshot we cannot evaluate
    return snapshot


def read_snapshot(snapshot: dict[str, Any]) -> RequestRules:
    """Parse a stored snapshot (v1 or v2) into evaluable rules. Raises on anything odd."""
    try:
        version = snapshot.get("schema_version", 1)
        if version == 1:
            primaries = [Seat(UUID(value)) for value in snapshot["approver_ids"]]
            rules = PolicyRules(
                ApprovalMode.UNANIMOUS,
                int(snapshot["required_approvals"]),
                VetoMode.ANY_REJECTION,
                tuple(primaries),
            )
            validate_rules(rules)
            return RequestRules(rules, (), None, None, 0)
        if version != SNAPSHOT_VERSION:
            raise InvalidPolicyError(f"unsupported snapshot version {version}")
        seats = tuple(
            Seat(UUID(item["approver_id"]), bool(item["can_veto"]))
            for item in snapshot["primaries"]
        )
        rules = PolicyRules(
            ApprovalMode(snapshot["approval_mode"]),
            int(snapshot["required_approvals"]),
            VetoMode(snapshot["veto_mode"]),
            seats,
        )
        validate_rules(rules)
        backups = tuple(UUID(value) for value in snapshot["backups"])
        if set(backups) & {seat.approver_id for seat in seats}:
            raise InvalidPolicyError("a backup cannot also be a primary approver")
        if snapshot.get("condition_rule") != CONDITION_RULE_REAPPROVAL:
            raise InvalidPolicyError("unsupported condition rule")
        if [str(s.approver_id) for s in seats] != snapshot["approver_ids"]:
            raise InvalidPolicyError("approver_ids does not match primaries")
        return RequestRules(
            rules,
            backups,
            snapshot["deadline_seconds"],
            snapshot["escalation_after_seconds"],
            int(snapshot["max_escalations"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, InvalidPolicyError):
            raise
        raise InvalidPolicyError(f"malformed policy snapshot: {exc}") from exc
