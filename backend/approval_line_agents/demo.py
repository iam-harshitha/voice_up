"""Demo agent: a simulated production deployment gated by human authorization.

The agent proposes ``deploy_to_production``, creates an approval request (through the
Approval Line MCP server by default, or the REST API), waits with bounded polling for
the authoritative decision, and runs a harmless simulated deployment only when an
explicit execution gate passes. The "deployment" writes a JSON execution record and
touches nothing else.

The backend's policy engine is the only authority. The gate is a second safety check
that refuses to act on anything other than an unambiguous, matching approval.

Exit codes:
    0   authorized; the simulated deployment ran (once)
    2   configuration error
    3   the approval request could not be created (nothing was executed)
    10  rejected
    11  expired (deadline passed)
    12  still not authorized when the wait timed out (pending / condition / re-approval)
    13  the backend's answer was malformed or contradictory
    14  the backend could not be reached reliably
    15  the gate refused an approval that did not match the intended action
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from approval_line_agents.client import (
    IDEMPOTENCY_KEY_PATTERN,
    ApiResponseError,
    ApprovalLineClient,
    ApprovalLineError,
    ApprovalStatus,
    BackendUnavailableError,
    ClientSettings,
    MalformedResponseError,
    parse_status,
)

ACTION = "deploy_to_production"
SUMMARY = (
    "Deploy release {release} to production. Automated tests passed, but the release "
    "includes a database migration. Verify the backup and migration safety before "
    "proceeding."
)

EXIT_EXECUTED = 0
EXIT_CONFIG = 2
EXIT_CREATE_FAILED = 3
EXIT_REJECTED = 10
EXIT_EXPIRED = 11
EXIT_TIMEOUT = 12
EXIT_MALFORMED = 13
EXIT_UNAVAILABLE = 14
EXIT_GATE_REFUSED = 15


# --- Gateways: how the agent talks to Approval Line --------------------------------------


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


def status_from_tool_result(structured: Any) -> ApprovalStatus:
    """Re-validate an MCP tool's structured result with the same strict parser as REST."""
    if not isinstance(structured, dict):
        raise MalformedResponseError("malformed_response", "Tool result is not an object.")
    evaluation_keys = (
        "state",
        "reason",
        "authorized",
        "next_action",
        "approvals",
        "rejections",
        "required_approvals",
        "outstanding_approver_ids",
        "condition_approver_ids",
        "reapproval_approver_ids",
        "deadline_passed",
        "expires_at",
    )
    missing = [k for k in (*evaluation_keys, "request_id", "status") if k not in structured]
    if missing:
        raise MalformedResponseError(
            "malformed_response", f"Tool result is missing {len(missing)} field(s)."
        )
    evaluation = {key: structured[key] for key in evaluation_keys}
    evaluation["veto_triggered"] = structured.get("reason") == "veto"
    return parse_status({**structured, "evaluation": evaluation})


def error_from_tool_text(text: str) -> ApprovalLineError:
    """Rebuild a typed error from the MCP server's error text (never a success)."""
    lowered = text.lower()
    if "backend_unavailable" in lowered:
        return BackendUnavailableError("backend_unavailable", text)
    if "untrusted_response" in lowered:
        return MalformedResponseError("untrusted_response", text)
    return ApiResponseError("tool_error", text)


class McpGateway:
    """Calls the Approval Line MCP server's tools over an MCP client session."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def _call(self, tool: str, arguments: dict[str, Any]) -> ApprovalStatus:
        result = await self._session.call_tool(tool, arguments)
        if result.is_error:
            text = " ".join(getattr(c, "text", "") for c in result.content).strip()
            raise error_from_tool_text(text or "tool error")
        return status_from_tool_result(result.structured_content)

    async def create(
        self,
        policy_id: UUID,
        action: str,
        summary: str,
        context: dict[str, Any],
        idempotency_key: str,
    ) -> ApprovalStatus:
        return await self._call(
            "request_approval",
            {
                "policy_id": str(policy_id),
                "action": action,
                "summary": summary,
                "context": context,
                "idempotency_key": idempotency_key,
            },
        )

    async def status(self, request_id: UUID) -> ApprovalStatus:
        status = await self._call("get_approval_status", {"request_id": str(request_id)})
        if status.request_id != request_id:
            raise MalformedResponseError(
                "request_mismatch", "The tool returned a different request than asked for."
            )
        return status


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


@dataclass
class WaitOutcome:
    exit_code: int
    message: str
    status: ApprovalStatus | None = None
    polls: int = 0


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


def say(line: str) -> None:
    """Progress output, flushed at once so it is visible even when piped or logged."""
    print(line, flush=True)


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
    report: Reporter,
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
                EXIT_MALFORMED, f"untrustworthy response: {exc.message}", last, polls
            )
        except BackendUnavailableError as exc:
            failures += 1
            report(f"  backend unavailable ({exc.code}); attempt {failures}")
            if failures >= config.max_transient_failures:
                return WaitOutcome(
                    EXIT_UNAVAILABLE, "cannot verify the decision: backend unavailable", last, polls
                )
        except ApprovalLineError as exc:
            return WaitOutcome(
                EXIT_UNAVAILABLE, f"backend refused the status check: {exc.code}", last, polls
            )
        else:
            last = status
            line = describe(status)
            if line != last_line:
                report(f"  {line}")
                last_line = line
            if status.status == "approved":
                return WaitOutcome(EXIT_EXECUTED, "approved", status, polls)
            if status.status == "rejected":
                return WaitOutcome(EXIT_REJECTED, "rejected; not deploying", status, polls)
            if status.status == "expired" or status.evaluation.deadline_passed:
                return WaitOutcome(EXIT_EXPIRED, "deadline passed; not deploying", status, polls)
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            return WaitOutcome(
                EXIT_TIMEOUT, "timed out while still not authorized; not deploying", last, polls
            )
        await clock.sleep(min(interval, remaining))
        interval = min(interval * config.backoff, config.max_interval)


# --- Harmless execution -----------------------------------------------------------------------


@dataclass
class SimulatedDeployment:
    """Writes one JSON execution record per authorized run. Deploys nothing."""

    record_dir: Path
    executions: list[Path] = field(default_factory=list)

    def __call__(self, status: ApprovalStatus, release: str) -> Path:
        self.record_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        path = self.record_dir / f"deploy-{status.request_id}.json"
        record = {
            "simulated": True,
            "action": status.action,
            "release": release,
            "target": "simulated-production (no real system is touched)",
            "authorized_by_request_id": str(status.request_id),
            "policy_id": str(status.policy_id),
            "policy_version": status.policy_version,
            "decided_at": status.decided_at.isoformat() if status.decided_at else None,
            "executed_at": now.isoformat(),
        }
        # "x" mode: a second execution for the same request fails instead of overwriting.
        with path.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2)
        self.executions.append(path)
        return path


Executor = Callable[[ApprovalStatus, str], Path]


async def run_agent(
    gateway: ApprovalGateway,
    *,
    policy_id: UUID,
    release: str,
    executor: Executor,
    config: WaitConfig,
    report: Reporter = say,
    resume_request_id: UUID | None = None,
    clock: Clock | None = None,
    idempotency_key: str | None = None,
) -> int:
    summary = SUMMARY.format(release=release)
    intended = IntendedAction(policy_id=policy_id, action=ACTION, summary=summary)
    context = {
        "release": release,
        "tests": "passed",
        "includes_database_migration": True,
        "backup_verified": False,
        "target": "simulated-production",
    }
    report(f"Proposed action: {ACTION}")
    report(f"  {summary}")

    if resume_request_id is None:
        # One key per intended action: retries (here, in the client, or a re-run of this
        # command with --idempotency-key) return the same request instead of a duplicate.
        key = idempotency_key or f"demo-{uuid4().hex}"
        report(f"Requesting human authorization from Approval Line (idempotency key {key})...")
        try:
            created = await gateway.create(policy_id, ACTION, summary, context, key)
        except ApprovalLineError as exc:
            report(f"Could not create the approval request ({exc.code}): {exc.message}")
            report(
                "Nothing was executed. If the request may have been created, re-run with "
                f"--idempotency-key {key} to recover it instead of creating a duplicate."
            )
            return EXIT_CREATE_FAILED
        request_id = created.request_id
        report(f"Approval request created: {request_id}")
        report(f"  {describe(created)}")
    else:
        request_id = resume_request_id
        report(f"Watching existing approval request {request_id}")

    report("Waiting for the authoritative decision (the agent will not act until then)...")
    outcome = await wait_for_decision(gateway, request_id, config, report, clock)
    if outcome.exit_code != EXIT_EXECUTED or outcome.status is None:
        report(f"HALTED: {outcome.message}")
        if outcome.status is not None:
            report(f"  final state: {describe(outcome.status)}")
        return outcome.exit_code

    decision = execution_gate(intended, request_id, outcome.status)
    if not decision.allowed:
        report(f"HALTED: execution gate refused: {decision.reason}")
        return EXIT_GATE_REFUSED
    try:
        path = executor(outcome.status, release)
    except FileExistsError:
        report("HALTED: this request already authorized one execution; not running it twice")
        return EXIT_GATE_REFUSED
    report(f"AUTHORIZED. Simulated deployment executed; record: {path}")
    return EXIT_EXECUTED


# --- CLI -------------------------------------------------------------------------------------


def _mcp_server_params(settings: ClientSettings) -> Any:
    from mcp.client.stdio import StdioServerParameters

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "approval_line_agents.mcp_server"],
        env={
            "APPROVAL_LINE_API_URL": settings.base_url,
            "APPROVAL_LINE_API_KEY": settings.api_key,
            "APPROVAL_LINE_TIMEOUT_SECONDS": str(settings.timeout_seconds),
            # Keep the server's diagnostics out of the agent's console unless something fails.
            "APPROVAL_LINE_MCP_LOG_LEVEL": os.environ.get("APPROVAL_LINE_MCP_LOG_LEVEL", "WARNING"),
        },
        cwd=str(Path(__file__).resolve().parents[1]),
    )


async def _main_async(args: argparse.Namespace) -> int:
    settings = ClientSettings.from_env()
    config = WaitConfig(
        poll_interval=args.poll_interval, max_interval=args.max_interval, timeout=args.timeout
    )
    executor = SimulatedDeployment(Path(args.record_dir))
    common = {
        "policy_id": args.policy_id,
        "release": args.release,
        "executor": executor,
        "config": config,
        "resume_request_id": args.resume,
        "idempotency_key": args.idempotency_key,
    }
    if args.transport == "rest":
        async with ApprovalLineClient(settings) as client:
            return await run_agent(RestGateway(client), **common)

    from mcp import Client

    say("Connecting to the Approval Line MCP server (stdio)...")
    async with Client(_mcp_server_params(settings)) as session:
        return await run_agent(McpGateway(session), **common)


def _positive(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def _idempotency_key(value: str) -> str:
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("8-128 characters: letters, digits, . _ : -")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="approval-line-demo-agent",
        description=(
            "Simulated production deployment gated by Approval Line. Needs "
            "APPROVAL_LINE_API_URL and APPROVAL_LINE_API_KEY (an agent key with "
            "requests:create,requests:read)."
        ),
    )
    parser.add_argument("--policy-id", type=UUID, required=True)
    parser.add_argument("--release", default="v2.4")
    parser.add_argument("--transport", choices=("mcp", "rest"), default="mcp")
    parser.add_argument(
        "--resume", type=UUID, metavar="REQUEST_ID", help="Watch an existing request instead."
    )
    parser.add_argument(
        "--idempotency-key",
        type=_idempotency_key,
        help="Reuse the key printed by an earlier run to recover its request (no duplicate).",
    )
    parser.add_argument("--poll-interval", type=_positive, default=2.0)
    parser.add_argument("--max-interval", type=_positive, default=15.0)
    parser.add_argument("--timeout", type=_positive, default=600.0, help="Overall wait (s).")
    parser.add_argument(
        "--record-dir",
        default=os.environ.get("DEMO_RECORD_DIR", ".demo_runs"),
        help="Where simulated execution records are written.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_main_async(args))
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:  # pragma: no cover
        print("interrupted; nothing was executed after this point", file=sys.stderr)
        return EXIT_TIMEOUT


if __name__ == "__main__":
    raise SystemExit(main())
