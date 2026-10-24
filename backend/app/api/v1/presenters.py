"""Builds response models that need server-side evaluation."""

from sqlalchemy.orm import Session

from app.db.models import ApprovalRequest
from app.schemas.request import ApprovalRequestRead, EvaluationRead
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


def request_read(session: Session, request: ApprovalRequest) -> ApprovalRequestRead:
    read = ApprovalRequestRead.model_validate(request)
    read.evaluation = evaluation_read(session, request)
    return read
