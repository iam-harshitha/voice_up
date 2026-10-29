"""Shared authorization logic for agents: the execution gate and bounded waiting.

Used by the demo agent (``demo.py``), the Claude Code execution guard (``guard.py``) and
the MCP server's ``wait_for_approval_decision`` tool. Nothing here computes a decision:
the backend's policy engine is the only authority. These helpers only refuse to treat
anything other than an unambiguous, matching approval as permission to act.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from approval_line_agents.client import (
    ApprovalLineClient,
    ApprovalLineError,
    ApprovalStatus,
    BackendUnavailableError,
    MalformedResponseError,
)

# --- Gateways: how an agent talks to Approval Line ----------------------------------------


class ApprovalGateway(Protocol):
    async def create(
        self,
        policy_id: UUID,
        action: str,
        summary: str,
        context: dict[str, Any],
        idempotency_key: str,
    ) -> ApprovalStatus: ...

    async def status(self, request_id: UUID) -> ApprovalStatus: ...


class RestGateway:
    def __init__(self, client: ApprovalLineClient) -> None:
        self._client = client

    async def create(
        self,
        policy_id: UUID,
        action: str,
        summary: str,
        context: dict[str, Any],
        idempotency_key: str,
    ) -> ApprovalStatus:
        return await self._client.create_request(
            policy_id, action, summary, context, idempotency_key=idempotency_key
        )

    async def status(self, request_id: UUID) -> ApprovalStatus:
        return await self._client.get_status(request_id)


# --- The execution gate -------------------------------------------------------------------


@dataclass(frozen=True)
class IntendedAction:
    """What the agent wants to do. Approval only counts if it is for exactly this."""

    policy_id: UUID
    action: str
    summary: str


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str


def execution_gate(
    intended: IntendedAction, request_id: UUID, status: ApprovalStatus
) -> GateDecision:
    """Second, independent check before acting. Every condition must hold."""
    e = status.evaluation
    checks = (
        (status.request_id == request_id, "request ID does not match the created request"),
        (status.action == intended.action, "approved action differs from the intended action"),
        (status.summary == intended.summary, "approved summary differs from the proposal"),
        (status.policy_id == intended.policy_id, "request is under a different policy"),
        (status.status == "approved", f"request status is {status.status!r}, not 'approved'"),
        (e.authorized is True, "backend did not report authorized=true"),
        (e.state == "approved" and e.reason == "quorum_met", "evaluation is not final approval"),
        (e.next_action == "none", "evaluation still requires an action"),
        (status.decided_at is not None, "request has no decision time"),
        # The deadline limits when a decision can be MADE, not when an approval may be
        # used: the backend refuses votes at or after expires_at, so a genuine approval
        # is always decided strictly before it. `evaluation.deadline_passed` only says
        # the clock is now past expires_at, which is also true for an approval observed
        # late (slow poll, --resume, agent restart), so it must not block execution.
        (
            status.expires_at is None
            or (status.decided_at is not None and status.decided_at < status.expires_at),
            "approval was not decided before the request's deadline",
        ),
    )
    for ok, reason in checks:
        if not ok:
            return GateDecision(False, reason)
    return GateDecision(True, "authorized by Approval Line")


# --- Waiting ---------------------------------------------------------------------------------


class Outcome(StrEnum):
    """How a bounded wait ended. Only APPROVED may lead to action, and only via the gate."""

    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    TIMEOUT = "timeout"  # still not authorized (pending / condition / re-approval)
    MALFORMED = "malformed"  # the backend's answer could not be trusted
    UNAVAILABLE = "unavailable"  # the backend could not be reached or refused the check


@dataclass
class WaitOutcome:
    outcome: Outcome
    message: str
    status: ApprovalStatus | None = None
    polls: int = 0
    error: ApprovalLineError | None = None


@dataclass
class WaitConfig:
    poll_interval: float = 2.0
    max_interval: float = 15.0
    backoff: float = 1.5
    timeout: float = 600.0
    max_transient_failures: int = 3


@dataclass
class Clock:
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


Reporter = Callable[[str], None]


def quiet(_line: str) -> None:
    """A reporter that discards progress lines."""


def describe(status: ApprovalStatus) -> str:
    e = status.evaluation
    text = (
        f"status={status.status} authorized={str(e.authorized).lower()} state={e.state} "
        f"reason={e.reason} next_action={e.next_action} "
        f"approvals={e.approvals}/{e.required_approvals}"
    )
    if e.condition_approver_ids:
        text += f" open_conditions={len(e.condition_approver_ids)}"
    if e.reapproval_approver_ids:
        text += f" awaiting_reapproval={len(e.reapproval_approver_ids)}"
    return text


async def wait_for_decision(
    gateway: ApprovalGateway,
    request_id: UUID,
    config: WaitConfig,
    report: Reporter = quiet,
    clock: Clock | None = None,
) -> WaitOutcome:
    """Poll until the request is final, the timeout passes, or something is untrustworthy.

    Never creates requests, never retries a creation, never treats an error as approval.
    """
    clock = clock or Clock()
    deadline = clock.monotonic() + config.timeout
    interval = config.poll_interval
    failures = 0
    polls = 0
    last: ApprovalStatus | None = None
    last_line = ""
    while True:
        try:
            status = await gateway.status(request_id)
            polls += 1
            failures = 0
        except MalformedResponseError as exc:
            return WaitOutcome(
                Outcome.MALFORMED, f"untrustworthy response: {exc.message}", last, polls, exc
            )
        except BackendUnavailableError as exc:
            failures += 1
            report(f"  backend unavailable ({exc.code}); attempt {failures}")
            if failures >= config.max_transient_failures:
                return WaitOutcome(
                    Outcome.UNAVAILABLE,
                    "cannot verify the decision: backend unavailable",
                    last,
                    polls,
                    exc,
                )
        except ApprovalLineError as exc:
            return WaitOutcome(
                Outcome.UNAVAILABLE,
                f"backend refused the status check: {exc.code}",
                last,
                polls,
                exc,
            )
        else:
            last = status
            line = describe(status)
            if line != last_line:
                report(f"  {line}")
                last_line = line
            if status.status == "approved":
                return WaitOutcome(Outcome.APPROVED, "approved", status, polls)
            if status.status == "rejected":
                return WaitOutcome(Outcome.REJECTED, "rejected", status, polls)
            if status.status == "expired" or status.evaluation.deadline_passed:
                return WaitOutcome(Outcome.EXPIRED, "deadline passed", status, polls)
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            return WaitOutcome(Outcome.TIMEOUT, "timed out while still not authorized", last, polls)
        await clock.sleep(min(interval, remaining))
        interval = min(interval * config.backoff, config.max_interval)
