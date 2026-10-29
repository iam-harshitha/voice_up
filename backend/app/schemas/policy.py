from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import EmailStr, Field, StringConstraints, model_validator

from app.schemas.common import ApiOutput, StrictInput, bounded_text

# E.164: a leading "+", a non-zero country code digit, and at most 15 digits in total.
E164PhoneNumber = Annotated[str, StringConstraints(pattern=r"^\+[1-9]\d{1,14}$")]

Name = bounded_text(200)
Description = bounded_text(2000)


class ApproverCreate(StrictInput):
    name: Name
    phone_number: E164PhoneNumber | None = None
    email: EmailStr | None = None

    @model_validator(mode="after")
    def _require_contact(self) -> Self:
        if self.phone_number is None and self.email is None:
            raise ValueError("at least one of phone_number or email is required")
        return self


class ApproverUpdate(StrictInput):
    name: Name | None = None
    phone_number: E164PhoneNumber | None = None
    email: EmailStr | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _reject_explicit_nulls(self) -> Self:
        for field in ("name", "is_active"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class ApproverRead(ApiOutput):
    id: UUID
    name: str
    phone_number: str | None
    email: str | None
    is_active: bool
    created_at: datetime


ApprovalModeField = Literal["unanimous", "quorum"]
VetoModeField = Literal["any_rejection", "designated", "none"]
DeadlineSeconds = Annotated[int, Field(ge=60, le=30 * 24 * 3600)]
EscalationSeconds = Annotated[int, Field(ge=60, le=7 * 24 * 3600)]


class PolicyCreate(StrictInput):
    """Defaults reproduce Chunk 1 behaviour: unanimous, any rejection vetoes, no backups,
    no deadline, no escalation."""

    name: Name
    description: Description | None = None
    approval_mode: ApprovalModeField = "unanimous"
    required_approvals: Literal[1, 2]
    # Primary approvers (the seats). Count and distinctness are checked by the policy
    # service so that every assignment problem produces `approval_policy_invalid`.
    approver_ids: list[UUID] = Field(min_length=1, max_length=10)
    veto_mode: VetoModeField = "any_rejection"
    veto_approver_ids: list[UUID] = Field(default_factory=list, max_length=10)
    backup_approver_ids: list[UUID] = Field(default_factory=list, max_length=10)
    decision_deadline_seconds: DeadlineSeconds | None = None
    escalation_after_seconds: EscalationSeconds | None = None
    max_escalations: Annotated[int, Field(ge=0, le=3)] | None = None
    # Opt-in: schedule the approvers' calls automatically when a request is created.
    auto_dial: bool = False


class PolicyUpdate(StrictInput):
    name: Name | None = None
    description: Description | None = None
    is_active: bool | None = None
    approval_mode: ApprovalModeField | None = None
    required_approvals: Literal[1, 2] | None = None
    approver_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=10)
    veto_mode: VetoModeField | None = None
    veto_approver_ids: list[UUID] | None = Field(default=None, max_length=10)
    backup_approver_ids: list[UUID] | None = Field(default=None, max_length=10)
    # null clears these (no deadline / no escalation).
    decision_deadline_seconds: DeadlineSeconds | None = None
    escalation_after_seconds: EscalationSeconds | None = None
    max_escalations: Annotated[int, Field(ge=0, le=3)] | None = None
    auto_dial: bool | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        for field in (
            "name",
            "is_active",
            "auto_dial",
            "approval_mode",
            "required_approvals",
            "approver_ids",
            "veto_mode",
            "veto_approver_ids",
            "backup_approver_ids",
            "max_escalations",
        ):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class PolicyApproverRead(ApiOutput):
    id: UUID
    name: str
    is_active: bool


class PolicyRead(ApiOutput):
    id: UUID
    name: str
    description: str | None
    approval_mode: str
    required_approvals: int
    veto_mode: str
    is_active: bool
    version: int
    approvers: list[PolicyApproverRead]
    veto_approver_ids: list[UUID]
    backup_approvers: list[PolicyApproverRead]
    decision_deadline_seconds: int | None
    escalation_after_seconds: int | None
    max_escalations: int
    auto_dial: bool
    created_at: datetime
    updated_at: datetime
