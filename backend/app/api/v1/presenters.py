"""Builds response models that need server-side evaluation."""

from sqlalchemy.orm import Session

from app.db.models import ApprovalRequest
from app.schemas.request import (
    ApprovalRequestRead,
    ApprovalRequestSummary,
    EvaluationRead,
    PolicySnapshotRead,
    RequestApproverRead,
)
from app.services import approval_service


def evaluation_read(session: Session, request: ApprovalRequest) -> EvaluationRead:
    evaluation = approval_service.evaluate(session, request)
    return EvaluationRead(
        state=evaluation.state.value,
        reason=evaluation.reason.value,
        authorized=evaluation.authorized,
        next_action=evaluation.next_action.value,
        approvals=evaluation.approvals,
        rejections=evaluation.rejections,
        required_approvals=evaluation.required_approvals,
        veto_triggered=evaluation.veto_triggered,
        outstanding_approver_ids=list(evaluation.outstanding_seats),
        condition_approver_ids=list(evaluation.condition_seats),
        reapproval_approver_ids=list(evaluation.reapproval_approvers),
        reapproval_seat_ids=list(evaluation.reapproval_seats),
        deadline_passed=evaluation.deadline_passed,
        expires_at=request.expires_at,
    )


def policy_snapshot_read(request: ApprovalRequest) -> PolicySnapshotRead:
    snapshot = request.policy_snapshot
    rules = approval_service.request_rules(request)
    return PolicySnapshotRead(
        schema_version=int(snapshot.get("schema_version", 1)),
        policy_id=request.policy_id,
        name=str(snapshot.get("name", "")),
        version=request.policy_version,
        approval_mode=rules.rules.mode.value,
        required_approvals=rules.rules.required_approvals,
        veto_mode=rules.rules.veto_mode.value,
        deadline_seconds=rules.deadline_seconds,
        escalation_after_seconds=rules.escalation_after_seconds,
        max_escalations=rules.max_escalations,
    )


def request_read(session: Session, request: ApprovalRequest) -> ApprovalRequestRead:
    read = ApprovalRequestRead.model_validate(request)
    read.evaluation = evaluation_read(session, request)
    read.policy = policy_snapshot_read(request)
    read.approvers = [
        RequestApproverRead(
            approver_id=row.approver_id,
            name=name,
            role=row.role,
            can_veto=row.can_veto,
            position=row.position,
        )
        for row, name in approval_service.list_request_approvers(session, request)
    ]
    return read


def request_summary(
    session: Session, request: ApprovalRequest, *, include_evaluation: bool
) -> ApprovalRequestSummary:
    summary = ApprovalRequestSummary.model_validate(request)
    if include_evaluation:
        summary.evaluation = evaluation_read(session, request)
    return summary
