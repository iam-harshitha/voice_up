from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 50
MAX_OFFSET = 10_000


def bounded_text(max_length: int) -> type[str]:
    """A string that is stripped, must not be blank, and is at most ``max_length`` long."""
    return Annotated[  # type: ignore[return-value]
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=max_length)
    ]


class StrictInput(BaseModel):
    """Base for write payloads: unknown fields are rejected rather than silently ignored."""

    model_config = ConfigDict(extra="forbid")


class ApiOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class ErrorDetail(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: list[ErrorDetail] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
