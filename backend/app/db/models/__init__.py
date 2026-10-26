"""Importing this package registers every model on Base.metadata (used by Alembic)."""

from app.db.models.api_key import ApiKey
from app.db.models.approval_policy import ApprovalPolicy, PolicyApprover
from app.db.models.approval_request import ApprovalRequest, RequestApprover
from app.db.models.approval_vote import ApprovalVote
from app.db.models.approver import Approver
from app.db.models.audit_event import AuditEvent
from app.db.models.dashboard import DashboardSession, DashboardUser
from app.db.models.organization import Organization
from app.db.models.voice import VoiceCall, VoiceJob, VoiceProviderEvent
from app.db.models.workflow import ApprovalCondition, EscalationStep

__all__ = [
    "ApiKey",
    "ApprovalCondition",
    "ApprovalPolicy",
    "ApprovalRequest",
    "ApprovalVote",
    "Approver",
    "AuditEvent",
    "DashboardSession",
    "DashboardUser",
    "EscalationStep",
    "Organization",
    "PolicyApprover",
    "RequestApprover",
    "VoiceCall",
    "VoiceJob",
    "VoiceProviderEvent",
]
