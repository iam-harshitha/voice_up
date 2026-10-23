"""Importing this package registers every model on Base.metadata (used by Alembic)."""

from app.db.models.api_key import ApiKey
from app.db.models.approval_policy import ApprovalPolicy, PolicyApprover
from app.db.models.approval_request import ApprovalRequest
from app.db.models.approval_vote import ApprovalVote
from app.db.models.approver import Approver
from app.db.models.audit_event import AuditEvent
from app.db.models.organization import Organization

__all__ = [
    "ApiKey",
    "ApprovalPolicy",
    "ApprovalRequest",
    "ApprovalVote",
    "Approver",
    "AuditEvent",
    "Organization",
    "PolicyApprover",
]
