"""Approval Line MCP server (stdio).

Exposes Approval Line to MCP-capable agents as a small set of tools. It is a thin
adapter over the authenticated REST API: it holds one least-privilege API key, computes
no decisions, never writes to the database and cannot vote, resolve conditions or change
a request's status.

Run:
    APPROVAL_LINE_API_URL=http://127.0.0.1:8000 APPROVAL_LINE_API_KEY=<agent key> \\
        python -m approval_line_agents.mcp_server

stdout carries the MCP protocol only; diagnostics go to stderr.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from approval_line_agents.authorization import Clock, RestGateway, WaitConfig, wait_for_decision
from approval_line_agents.client import (
    ApiResponseError,
    ApprovalLineClient,
    ApprovalLineError,
    ApprovalStatus,
    BackendUnavailableError,
    ClientSettings,
    MalformedResponseError,
)

logger = logging.getLogger("approval_line_agents.mcp_server")

# One wait_for_approval_decision call stays well below common MCP tool-call timeouts;
# the agent calls it again while the request is still pending.
MAX_WAIT_SECONDS = 60
DEFAULT_WAIT_SECONDS = 30

SERVER_NAME = "approval-line"
INSTRUCTIONS = (
    "Approval Line provides human authorization for consequential actions. Call "
    "request_approval before acting, then wait_for_approval_decision (or "
    "get_approval_status) until the request is final. "
    "Proceed ONLY when the result has authorized=true and status='approved' for the same "
    "request_id and action. pending, rejected, expired and condition-blocked results mean "
    "do not act. Errors mean the authorization state is unknown: do not act."
)

PolicyId = Annotated[UUID, Field(description="Approval policy ID (UUID).")]
RequestId = Annotated[UUID, Field(description="Approval request ID (UUID).")]
ActionText = Annotated[
    str, Field(min_length=1, max_length=200, description="Short action identifier.")
]
SummaryText = Annotated[
    str,
    Field(min_length=1, max_length=2000, description="What will happen and why, for approvers."),
]
StatusFilter = Literal["pending", "approved", "rejected", "expired"]
IdempotencyKeyArg = Annotated[
    str | None,
    Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:\-]+$",
        description=(
            "Optional, recommended. A key you choose for this intended action (e.g. a UUID). "
            "If a call fails or times out, call again with the SAME key: you get the original "
            "request back instead of a duplicate."
        ),
    ),
]


class ApprovalStatusResult(BaseModel):
    """The backend's authoritative answer for one request."""

    request_id: UUID
    action: str
    summary: str
    policy_id: UUID
    policy_version: int
    status: Literal["pending", "approved", "rejected", "expired"]
    authorized: bool = Field(description="True only when the request is approved.")
    state: str
    reason: str
    next_action: str
    approvals: int
    rejections: int
    required_approvals: int
    outstanding_approver_ids: list[UUID]
    condition_approver_ids: list[UUID]
    reapproval_approver_ids: list[UUID]
    deadline_passed: bool
    expires_at: datetime | None
    decided_at: datetime | None
    created_at: datetime
    guidance: str = Field(description="Plain-language instruction for the agent.")


class RequestListItem(BaseModel):
    request_id: UUID
    action: str
    summary: str
    status: str
    created_at: datetime
    decided_at: datetime | None


class RequestListResult(BaseModel):
    items: list[RequestListItem]
    total: int
    limit: int
    offset: int


class TimelineEventResult(BaseModel):
    sequence: int
    event_type: str
    created_at: datetime
    event_data: dict[str, Any]


class TimelineResult(BaseModel):
    request_id: UUID
    items: list[TimelineEventResult]
    next_after: int | None


class PolicyApproverResult(BaseModel):
    id: UUID
    name: str
    is_active: bool


class PolicyResult(BaseModel):
    policy_id: UUID
    name: str
    description: str | None
    version: int
    is_active: bool
    approval_mode: str
    required_approvals: int
    veto_mode: str
    approvers: list[PolicyApproverResult]
    veto_approver_ids: list[UUID]
    backup_approvers: list[PolicyApproverResult]
    decision_deadline_seconds: int | None
    escalation_after_seconds: int | None
    max_escalations: int
    auto_dial: bool | None = Field(
        description=(
            "True: approvers are called automatically when a request is created. False: an "
            "operator must schedule the calls. Null: not reported by this backend."
        )
    )


def guidance_for(status: ApprovalStatus) -> str:
    evaluation = status.evaluation
    if status.status == "approved" and evaluation.authorized:
        return "Authorized: the policy is satisfied. You may perform exactly this action."
    if status.status == "rejected":
        return f"Rejected ({evaluation.reason}). Do not perform the action."
    if status.status == "expired" or evaluation.deadline_passed:
        return "The decision deadline passed. Do not perform the action."
    if evaluation.reason == "condition_open":
        return (
            "An approver set a condition that must be resolved by an authorized person. "
            "Not authorized; do not perform the action yet."
        )
    if evaluation.reason == "reapproval_required":
        return (
            "A condition was satisfied but the approver must re-approve unconditionally. "
            "Not authorized; do not perform the action yet."
        )
    return (
        f"Pending: {evaluation.approvals} of {evaluation.required_approvals} required "
        "approvals. Not authorized; keep waiting."
    )


def status_result(status: ApprovalStatus) -> ApprovalStatusResult:
    e = status.evaluation
    return ApprovalStatusResult(
        request_id=status.request_id,
        action=status.action,
        summary=status.summary,
        policy_id=status.policy_id,
        policy_version=status.policy_version,
        status=status.status,
        authorized=e.authorized,
        state=e.state,
        reason=e.reason,
        next_action=e.next_action,
        approvals=e.approvals,
        rejections=e.rejections,
        required_approvals=e.required_approvals,
        outstanding_approver_ids=e.outstanding_approver_ids,
        condition_approver_ids=e.condition_approver_ids,
        reapproval_approver_ids=e.reapproval_approver_ids,
        deadline_passed=e.deadline_passed,
        expires_at=e.expires_at,
        decided_at=status.decided_at,
        created_at=status.created_at,
        guidance=guidance_for(status),
    )


def tool_error(exc: ApprovalLineError) -> ToolError:
    """Map a client failure to a safe, machine-readable tool error (never a success)."""
    if isinstance(exc, BackendUnavailableError):
        text = (
            f"backend_unavailable ({exc.code}): {exc.message} "
            "The authorization state is unknown; do not act."
        )
    elif isinstance(exc, MalformedResponseError):
        text = f"untrusted_response ({exc.code}): {exc.message} Do not act."
    elif isinstance(exc, ApiResponseError):
        category = {
            401: "authentication_failed",
            403: "permission_denied",
            404: "not_found",
            409: "conflict",
            422: "invalid_input",
            429: "rate_limited",
        }.get(exc.status_code or 0, "request_refused")
        text = f"{category} ({exc.code}): {exc.message}"
    else:  # pragma: no cover - every client error is one of the above
        text = f"error ({exc.code}): {exc.message}"
    return ToolError(text)


def build_server(client: ApprovalLineClient, *, clock: Clock | None = None) -> MCPServer:
    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS, version="0.5.0")

    async def run(tool: str, coro: Any) -> Any:
        try:
            result = await coro
        except ApprovalLineError as exc:
            logger.info("tool_failed", extra={"tool": tool, "code": exc.code})
            raise tool_error(exc) from None
        logger.info("tool_succeeded", extra={"tool": tool})
        return result

    @server.tool(
        description=(
            "Ask humans to authorize a consequential action. Creates a durable approval "
            "request under the given policy and returns its initial, authoritative state "
            "(normally pending). Creating a request never authorizes anything. To retry after "
            "an error, call again with the same idempotency_key; never retry without one."
        )
    )
    async def request_approval(
        policy_id: PolicyId,
        action: ActionText,
        summary: SummaryText,
        context: Annotated[
            dict[str, Any] | None,
            Field(description="Structured facts for approvers (JSON object, at most 16 KB)."),
        ] = None,
        idempotency_key: IdempotencyKeyArg = None,
    ) -> ApprovalStatusResult:
        # Without a caller key, a per-call key still makes this server's own retries of an
        # ambiguous failure safe (the API replays instead of creating a duplicate).
        key = idempotency_key or f"mcp-{uuid4().hex}"
        status = await run(
            "request_approval",
            client.create_request(policy_id, action, summary, context, idempotency_key=key),
        )
        return status_result(status)

    @server.tool(
        description=(
            "Get the authoritative state of an approval request: status, authorized, the "
            "policy evaluation (reason, next_action, approvals so far) and guidance. Only "
            "authorized=true with status='approved' permits the action."
        )
    )
    async def get_approval_status(request_id: RequestId) -> ApprovalStatusResult:
        return status_result(await run("get_approval_status", client.get_status(request_id)))

    @server.tool(
        description="List this organization's approval requests, newest first (paginated)."
    )
    async def list_approval_requests(
        status: Annotated[StatusFilter | None, Field(description="Filter by status.")] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        offset: Annotated[int, Field(ge=0, le=10_000)] = 0,
    ) -> RequestListResult:
        page = await run(
            "list_approval_requests",
            client.list_requests(status=status, limit=limit, offset=offset),
        )
        return RequestListResult(
            items=[
                RequestListItem(
                    request_id=item.request_id,
                    action=item.action,
                    summary=item.summary,
                    status=item.status,
                    created_at=item.created_at,
                    decided_at=item.decided_at,
                )
                for item in page.items
            ],
            total=page.total,
            limit=page.limit,
            offset=page.offset,
        )

    @server.tool(
        description=(
            "Get the append-only audit timeline of an approval request (votes, conditions, "
            "escalations, calls, final decision), in order. Requires the audit:read scope."
        )
    )
    async def get_approval_timeline(
        request_id: RequestId,
        after: Annotated[int, Field(ge=0, description="Return events after this sequence.")] = 0,
        limit: Annotated[int, Field(ge=1, le=200)] = 100,
    ) -> TimelineResult:
        timeline = await run(
            "get_approval_timeline", client.get_timeline(request_id, after=after, limit=limit)
        )
        return TimelineResult(
            request_id=timeline.request_id,
            items=[
                TimelineEventResult(
                    sequence=e.sequence,
                    event_type=e.event_type,
                    created_at=e.created_at,
                    event_data=e.event_data,
                )
                for e in timeline.items
            ],
            next_after=timeline.next_after,
        )

    @server.tool(
        description=(
            "Get an approval policy's rules (mode, required approvals, veto, approvers, "
            "backups, deadline). Requires the policies:read scope."
        )
    )
    async def get_approval_policy(policy_id: PolicyId) -> PolicyResult:
        policy = await run("get_approval_policy", client.get_policy(policy_id))
        return PolicyResult(
            policy_id=policy.id,
            name=policy.name,
            description=policy.description,
            version=policy.version,
            is_active=policy.is_active,
            approval_mode=policy.approval_mode,
            required_approvals=policy.required_approvals,
            veto_mode=policy.veto_mode,
            approvers=[PolicyApproverResult(**a.model_dump()) for a in policy.approvers],
            veto_approver_ids=policy.veto_approver_ids,
            backup_approvers=[
                PolicyApproverResult(**a.model_dump()) for a in policy.backup_approvers
            ],
            decision_deadline_seconds=policy.decision_deadline_seconds,
            escalation_after_seconds=policy.escalation_after_seconds,
            max_escalations=policy.max_escalations,
            auto_dial=policy.auto_dial,
        )

    @server.tool(
        description=(
            "Wait (bounded) for an approval request to become final, then return its "
            "authoritative state. Returns as soon as it is approved, rejected or expired; if "
            f"max_wait_seconds (at most {MAX_WAIT_SECONDS}) passes first, returns the current "
            "pending state: call this tool again to keep waiting. Only authorized=true with "
            "status='approved' permits the action."
        )
    )
    async def wait_for_approval_decision(
        request_id: RequestId,
        max_wait_seconds: Annotated[
            int,
            Field(ge=1, le=MAX_WAIT_SECONDS, description="Upper bound for this call (s)."),
        ] = DEFAULT_WAIT_SECONDS,
    ) -> ApprovalStatusResult:
        config = WaitConfig(poll_interval=2.0, max_interval=10.0, timeout=float(max_wait_seconds))
        outcome = await wait_for_decision(RestGateway(client), request_id, config, clock=clock)
        if outcome.error is not None:
            # Malformed, unreachable or refused: the state is unknown; never a success.
            logger.info(
                "tool_failed",
                extra={"tool": "wait_for_approval_decision", "code": outcome.error.code},
            )
            raise tool_error(outcome.error)
        if outcome.status is None:
            # The wait ended before any poll succeeded (transient outage).
            raise ToolError(
                "backend_unavailable (no_status): the state could not be read before the wait "
                "ended. The authorization state is unknown; do not act."
            )
        logger.info(
            "tool_succeeded",
            extra={"tool": "wait_for_approval_decision", "outcome": outcome.outcome.value},
        )
        return status_result(outcome.status)

    return server


async def _serve() -> None:
    settings = ClientSettings.from_env()
    async with ApprovalLineClient(settings) as client:
        server = build_server(client)
        logger.info("mcp_server_started", extra={"backend": settings.base_url})
        await server.run_stdio_async()


def main() -> int:
    # stdout is the protocol stream: all diagnostics go to stderr.
    level = os.environ.get("APPROVAL_LINE_MCP_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=level if level in logging.getLevelNamesMapping() else "INFO",
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Per-request HTTP lines add noise and nothing an operator needs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(_serve())
    except ValueError as exc:  # configuration problems; never includes the key
        print(f"approval-line MCP server: configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
