"""Approval Line execution guard: a Claude Code PreToolUse hook for shell commands.

Claude Code runs this before every Bash or PowerShell command (see
docs/claude-code-guard.md; on Windows the PowerShell tool is on by default and is the
primary shell, so guarding Bash alone would be bypassable). A command
that matches a rule in the rules file is not run until a human approves it through
Approval Line: the guard creates an approval request under the rule's policy (whose
``auto_dial`` setting makes the backend phone the approvers), waits for the
authoritative decision, and lets the command run only if the execution gate passes for
exactly this command. Commands that match no rule pass at once, with no network call.

Hook contract (code.claude.com/docs/en/hooks, verified 2026-10-29):
    stdin   JSON with session_id, cwd, hook_event_name, tool_name, tool_input.command,
            tool_use_id
    exit 0  allow: the call continues through Claude Code's normal permission flow
    exit 2  block: stderr is shown to Claude as the reason. This happens before
            permission rules are evaluated, so an allow rule can never bypass it
            (code.claude.com/docs/en/permissions, "Extend permissions with hooks")
A command hook that crashes, exits with another code or times out does NOT block on its
own, so this guard catches every failure and exits 2 itself, and the hook is registered
with ``"onFailure": "block"`` and a timeout longer than the guard's wait.

Environment:
    APPROVAL_LINE_API_URL, APPROVAL_LINE_API_KEY   agent key (requests:create,
                                                   requests:read; policies:read for the
                                                   auto-dial preflight)
    APPROVAL_LINE_GUARD_RULES                      path of the JSON rules file
    APPROVAL_LINE_GUARD_TIMEOUT_SECONDS            total wait for a decision (default 300)
    APPROVAL_LINE_GUARD_ALLOW_MANUAL_DIAL=1        allow policies with auto-dial off

Fail closed: anything other than an approval that passes the gate blocks the command.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import string
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from approval_line_agents.authorization import (
    Clock,
    IntendedAction,
    Outcome,
    RestGateway,
    WaitConfig,
    execution_gate,
    wait_for_decision,
)
from approval_line_agents.client import (
    IDEMPOTENCY_KEY_PATTERN,
    ApprovalLineClient,
    ApprovalLineError,
    ApprovalStatus,
    ClientSettings,
)

EXIT_ALLOW = 0
EXIT_BLOCK = 2

ENV_RULES = "APPROVAL_LINE_GUARD_RULES"
ENV_TIMEOUT = "APPROVAL_LINE_GUARD_TIMEOUT_SECONDS"
ENV_ALLOW_MANUAL_DIAL = "APPROVAL_LINE_GUARD_ALLOW_MANUAL_DIAL"

DEFAULT_TIMEOUT_SECONDS = 300.0
MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS = 5.0, 3000.0
MAX_STDIN_BYTES = 1_000_000
MAX_RULES_BYTES = 256_000
MAX_RULES = 100
# Bounds for what reaches approvers (the API limits context to 16 KB, summaries to 2000).
MAX_COMMAND_CONTEXT_BYTES = 4000
MAX_CWD_CONTEXT_BYTES = 500
MAX_COMMAND_IN_SUMMARY = 300
MAX_SUMMARY = 2000
MAX_ACTION = 200
SUMMARY_FIELDS = frozenset({"command", "cwd", "rule"})
BLOCK_PREFIX = "Approval Line guard: "
# Appended to every block so Claude relays the decision instead of looping or routing
# around it (a retry is a new tool call: a new request and a new phone call).
STOP_INSTRUCTION = (
    "STOP: report this reason and the request ID to the user and wait for their "
    "instructions. Do NOT retry this command, and do NOT look for a workaround or an "
    "alternative command to achieve the same result."
)
# Shell tools whose tool_input.command is checked against the rules.
GUARDED_TOOLS = frozenset({"Bash", "PowerShell"})


class GuardBlockError(Exception):
    """The command must not run. ``message`` is plain language and safe to show Claude."""

    def __init__(self, message: str, request_id: UUID | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.request_id = request_id


# --- Rules -----------------------------------------------------------------------------------


class Rule(BaseModel):
    """One guarded command pattern and the policy that must approve it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    pattern: Annotated[str, Field(min_length=1, max_length=1000)]
    policy_id: UUID
    action: Annotated[str, Field(min_length=1, max_length=MAX_ACTION)]
    summary: Annotated[str, Field(min_length=1, max_length=MAX_SUMMARY)]
    # Shell tools the rule applies to; default: every guarded shell.
    tools: Annotated[list[Literal["Bash", "PowerShell"]], Field(min_length=1)] | None = None

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from None
        return value

    @field_validator("action")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("summary")
    @classmethod
    def _known_placeholders(cls, value: str) -> str:
        try:
            names = {f for _, f, _, _ in string.Formatter().parse(value) if f is not None}
        except ValueError as exc:
            raise ValueError(f"invalid summary template: {exc}") from None
        unknown = names - SUMMARY_FIELDS
        if unknown:
            raise ValueError(
                f"unknown placeholder(s) {sorted(unknown)}; use {sorted(SUMMARY_FIELDS)}"
            )
        return value


class RulesFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rules: Annotated[list[Rule], Field(min_length=1, max_length=MAX_RULES)]


@dataclass(frozen=True)
class CompiledRule:
    rule: Rule
    regex: re.Pattern[str]

    @property
    def label(self) -> str:
        return self.rule.name or self.rule.action


def load_rules(path_value: str | None) -> list[CompiledRule]:
    """Read and validate the rules file. Any problem raises ``GuardBlockError``."""
    if not path_value or not path_value.strip():
        raise GuardBlockError(f"{ENV_RULES} is not set, so no command can be checked.")
    path = Path(path_value.strip())
    try:
        raw = path.read_bytes()
    except OSError:
        raise GuardBlockError(f"the rules file {path} cannot be read.") from None
    if len(raw) > MAX_RULES_BYTES:
        raise GuardBlockError(f"the rules file {path} is larger than {MAX_RULES_BYTES} bytes.")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
        parsed = RulesFile.model_validate(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GuardBlockError(f"the rules file {path} is not valid JSON.") from None
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise GuardBlockError(
            f"the rules file {path} is invalid ({exc.error_count()} problem(s); first: "
            f"{where}: {first['msg']})."
        ) from None
    return [CompiledRule(rule, re.compile(rule.pattern)) for rule in parsed.rules]


def match_rule(
    rules: list[CompiledRule], command: str, tool_name: str = "Bash"
) -> CompiledRule | None:
    """First rule for this shell whose pattern is found anywhere in the command."""
    return next(
        (
            rule
            for rule in rules
            if (rule.rule.tools is None or tool_name in rule.rule.tools)
            and rule.regex.search(command)
        ),
        None,
    )


# --- Hook input ------------------------------------------------------------------------------


@dataclass(frozen=True)
class HookInput:
    tool_name: str
    command: str | None
    cwd: str
    session_id: str
    tool_use_id: str | None


def parse_hook_input(raw: bytes) -> HookInput:
    if len(raw) > MAX_STDIN_BYTES:
        raise GuardBlockError("the hook input is too large to check.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GuardBlockError("the hook input from Claude Code is not valid JSON.") from None
    if not isinstance(data, dict) or not isinstance(data.get("tool_name"), str):
        raise GuardBlockError("the hook input from Claude Code has no tool_name.")
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None

    def text(key: str) -> str:
        value = data.get(key)
        return value if isinstance(value, str) else ""

    tool_use_id = data.get("tool_use_id")
    return HookInput(
        tool_name=data["tool_name"],
        command=command if isinstance(command, str) else None,
        cwd=text("cwd"),
        session_id=text("session_id"),
        tool_use_id=tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None,
    )


def idempotency_key(hook: HookInput) -> str:
    """Stable per tool call, so a re-invoked hook replays the same request (and call)."""
    if hook.tool_use_id:
        candidate = f"guard-{hook.tool_use_id}"
        if IDEMPOTENCY_KEY_PATTERN.fullmatch(candidate):
            return candidate
        digest = hashlib.sha256(hook.tool_use_id.encode()).hexdigest()
        return f"guard-tu-{digest[:48]}"
    digest = hashlib.sha256(f"{hook.session_id}\0{hook.command}".encode()).hexdigest()
    return f"guard-sc-{digest[:48]}"


# --- The request -----------------------------------------------------------------------------


def truncate_chars(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = f"... [{len(text) - limit} more characters]"
    return text[: max(0, limit - len(marker))] + marker


def truncate_bytes(text: str, limit: int) -> tuple[str, bool]:
    """Cut to at most ``limit`` UTF-8 bytes without splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    marker = " ... [truncated]"
    kept = encoded[: max(0, limit - len(marker))].decode("utf-8", errors="ignore")
    return kept + marker, True


@dataclass(frozen=True)
class GuardRequest:
    intended: IntendedAction
    context: dict[str, Any]
    idempotency_key: str


def build_request(rule: CompiledRule, hook: HookInput) -> GuardRequest:
    """Deterministic for one tool call (no timestamps), so a replay matches exactly.

    Only the command, its working directory and the rule name are sent: never environment
    variables. The API strips action and summary, so they are stripped here too and the
    gate's exact comparison holds.
    """
    command = hook.command or ""
    summary = rule.rule.summary.format(
        command=truncate_chars(command, MAX_COMMAND_IN_SUMMARY),
        cwd=truncate_chars(hook.cwd, 200),
        rule=rule.label,
    )
    summary = truncate_chars(summary.strip(), MAX_SUMMARY).strip()
    action = rule.rule.action.strip()
    if not summary:
        raise GuardBlockError(f"rule {rule.label!r} produced an empty summary.")
    command_text, command_truncated = truncate_bytes(command, MAX_COMMAND_CONTEXT_BYTES)
    cwd_text, _ = truncate_bytes(hook.cwd, MAX_CWD_CONTEXT_BYTES)
    context: dict[str, Any] = {
        "command": command_text,
        "working_directory": cwd_text,
        "guard_rule": rule.label,
        "shell": hook.tool_name,
        "requested_by": "Claude Code (Approval Line guard)",
    }
    if command_truncated:
        context["command_truncated"] = True
    return GuardRequest(
        IntendedAction(rule.rule.policy_id, action, summary), context, idempotency_key(hook)
    )


# --- Running the guard ------------------------------------------------------------------------


@dataclass
class GuardResult:
    exit_code: int
    message: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def stderr(self) -> str:
        return "\n".join(line for line in (self.message, *self.warnings) if line)


def _timeout(env: Mapping[str, str]) -> float:
    raw = env.get(ENV_TIMEOUT, "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise GuardBlockError(f"{ENV_TIMEOUT} must be a number of seconds.") from None
    if not MIN_TIMEOUT_SECONDS <= value <= MAX_TIMEOUT_SECONDS:
        raise GuardBlockError(
            f"{ENV_TIMEOUT} must be between {MIN_TIMEOUT_SECONDS:g} and "
            f"{MAX_TIMEOUT_SECONDS:g} seconds."
        )
    return value


async def _preflight_auto_dial(
    client: ApprovalLineClient, policy_id: UUID, allow_manual: bool, warnings: list[str]
) -> None:
    """Advisory: refuse a policy that would call no one. Unreadable policy -> warn only."""
    try:
        policy = await client.get_policy(policy_id)
    except ApprovalLineError as exc:
        warnings.append(
            f"warning: could not read policy {policy_id} to check auto-dial ({exc.code}); "
            "continuing. Give the agent key policies:read to enable this check."
        )
        return
    if policy.auto_dial is None:
        warnings.append(
            f"warning: policy {policy_id} does not report auto_dial (older backend); continuing."
        )
        return
    if policy.auto_dial:
        return
    if allow_manual:
        warnings.append(
            f"warning: policy '{policy.name}' has auto-dial off; continuing because "
            f"{ENV_ALLOW_MANUAL_DIAL}=1. An operator must click Schedule calls."
        )
        return
    raise GuardBlockError(
        f"policy '{policy.name}' ({policy_id}) has auto-dial off, so no one would be called. "
        "Command not run. Turn on auto-dial for that policy, or set "
        f"{ENV_ALLOW_MANUAL_DIAL}=1 if an operator will schedule the calls by hand."
    )


def _outcome_message(outcome: Outcome, status: ApprovalStatus | None, timeout: float) -> str:
    if outcome is Outcome.REJECTED:
        return "Rejected by an approver. The command was not run."
    if outcome is Outcome.EXPIRED:
        return "The approval deadline passed without approval. The command was not run."
    if outcome is Outcome.TIMEOUT:
        reason = status.evaluation.reason if status is not None else ""
        detail = {
            "condition_open": " An approver set a condition that is still unresolved.",
            "reapproval_required": " A condition was met but the approver must re-approve.",
        }.get(reason, "")
        return (
            f"No decision before the timeout ({timeout:g} s).{detail} The command was not "
            "run. A retry would create a new request and phone the approver again."
        )
    if outcome is Outcome.MALFORMED:
        return "Approval Line returned an untrustworthy answer. The command was not run."
    return "Approval Line unreachable, the command was not run."


async def run_guard(
    raw_input: bytes,
    env: Mapping[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock | None = None,
) -> GuardResult:
    """Decide one hook invocation. Every path that is not a verified approval blocks."""
    warnings: list[str] = []
    request_id: UUID | None = None
    try:
        hook = parse_hook_input(raw_input)
        if hook.tool_name not in GUARDED_TOOLS:
            return GuardResult(EXIT_ALLOW)  # only shell commands are guarded
        if hook.command is None:
            raise GuardBlockError(f"the {hook.tool_name} tool call has no command to check.")
        rules = load_rules(env.get(ENV_RULES))
        rule = match_rule(rules, hook.command, hook.tool_name)
        if rule is None:
            return GuardResult(EXIT_ALLOW)  # not guarded: no network call

        timeout = _timeout(env)
        try:
            settings = ClientSettings.from_env_mapping(env)
        except ValueError as exc:
            raise GuardBlockError(f"configuration error: {exc}.") from None
        request = build_request(rule, hook)
        allow_manual = env.get(ENV_ALLOW_MANUAL_DIAL, "").strip() == "1"

        async with ApprovalLineClient(settings, transport=transport) as client:
            await _preflight_auto_dial(client, request.intended.policy_id, allow_manual, warnings)
            try:
                created = await client.create_request(
                    request.intended.policy_id,
                    request.intended.action,
                    request.intended.summary,
                    request.context,
                    idempotency_key=request.idempotency_key,
                )
            except ApprovalLineError as exc:
                raise GuardBlockError(
                    f"the approval request could not be created ({exc.code}). The command "
                    "was not run."
                ) from None
            request_id = created.request_id
            config = WaitConfig(poll_interval=2.0, max_interval=10.0, timeout=timeout)
            outcome = await wait_for_decision(RestGateway(client), request_id, config, clock=clock)

        if outcome.outcome is not Outcome.APPROVED or outcome.status is None:
            raise GuardBlockError(
                _outcome_message(outcome.outcome, outcome.status, timeout), request_id
            )
        decision = execution_gate(request.intended, request_id, outcome.status)
        if not decision.allowed:
            raise GuardBlockError(
                f"the approval does not match this command ({decision.reason}). "
                "The command was not run.",
                request_id,
            )
        return GuardResult(EXIT_ALLOW, warnings=warnings)
    except GuardBlockError as block:
        return _blocked(block.message, block.request_id or request_id, warnings)
    except Exception as exc:  # fail closed on anything unexpected
        return _blocked(
            f"unexpected error ({type(exc).__name__}). The command was not run.",
            request_id,
            warnings,
        )


def _blocked(message: str, request_id: UUID | None, warnings: list[str]) -> GuardResult:
    suffix = f" (approval request {request_id})" if request_id else ""
    return GuardResult(EXIT_BLOCK, f"{BLOCK_PREFIX}{message}{suffix}\n{STOP_INSTRUCTION}", warnings)


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
        result = asyncio.run(run_guard(raw, os.environ))
    except BaseException as exc:  # including KeyboardInterrupt: never let a command through
        result = _blocked(f"guard failed ({type(exc).__name__}).", None, [])
    if result.stderr:
        sys.stderr.write(result.stderr + "\n")
        sys.stderr.flush()
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
