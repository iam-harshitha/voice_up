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


class PolicyCreate(StrictInput):
    name: Name
    description: Description | None = None
    required_approvals: Literal[1, 2]
    # Count and distinctness are checked by the policy service so that every assignment
    # problem produces the same `approval_policy_invalid` error.
    approver_ids: list[UUID] = Field(min_length=1, max_length=10)


class PolicyUpdate(StrictInput):
    name: Name | None = None
    description: Description | None = None
    is_active: bool | None = None
    required_approvals: Literal[1, 2] | None = None
    approver_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=10)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        for field in ("name", "is_active", "required_approvals", "approver_ids"):
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
    required_approvals: int
    is_active: bool
    version: int
    approvers: list[PolicyApproverRead]
    created_at: datetime
    updated_at: datetime
