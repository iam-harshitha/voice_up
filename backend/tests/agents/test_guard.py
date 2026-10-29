"""Claude Code execution guard: fail closed on every path that is not a verified approval.

The backend is a scripted fake behind httpx.MockTransport, so the real client (strict
parsing, idempotency) and the real gate and waiting code run. No network, no phone calls.
The end-to-end run against the real API and mock voice provider is in
tests/integration/test_guard_e2e.py.
"""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest

from approval_line_agents import guard
from approval_line_agents.guard import (
    ENV_ALLOW_MANUAL_DIAL,
    ENV_RULES,
    ENV_TIMEOUT,
    EXIT_ALLOW,
    EXIT_BLOCK,
    MAX_COMMAND_CONTEXT_BYTES,
    STOP_INSTRUCTION,
    idempotency_key,
    parse_hook_input,
    run_guard,
)
from tests.agents.payloads import POLICY_ID, request_payload

SECRET = "al_guard_test_secret_key_never_shown"
DEPLOY = "./deploy.sh production"
ACTION = "deploy_to_production"
SUMMARY_TEMPLATE = "Claude Code wants to run `{command}` in {cwd} ({rule})."
CWD = "D:/demo-target"


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


class FakeBackend:
    """A minimal Approval Line: policies, idempotent request creation, scripted polls."""

    def __init__(self, script=("approved",), *, auto_dial=True, policy_status=200, **overrides):
        self.script = list(script)
        self.auto_dial = auto_dial
        self.policy_status = policy_status
        self.overrides = overrides  # applied to every status payload (e.g. action=...)
        self.requests: dict[str, dict] = {}  # idempotency key -> created request
        self.posts: list[dict] = []
        self.calls: list[str] = []
        self.down = False

    @property
    def created(self) -> dict:
        (only,) = self.requests.values()
        return only

    def payload(self, kind, request):
        fields = {"action": request["action"], "summary": request["summary"], **self.overrides}
        return request_payload(kind, request["id"], policy_id=request["policy_id"], **fields)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.path}")
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        assert request.headers["X-API-Key"] == SECRET
        path = request.url.path
        if request.method == "GET" and path.startswith("/api/v1/policies/"):
            if self.policy_status != 200:
                return httpx.Response(
                    self.policy_status,
                    json={"error": {"code": "insufficient_scope", "message": "missing scope"}},
                )
            body = {
                "id": path.rsplit("/", 1)[1],
                "name": "Production deploys",
                "description": None,
                "approval_mode": "unanimous",
                "required_approvals": 1,
                "veto_mode": "any_rejection",
                "is_active": True,
                "version": 1,
                "approvers": [],
                "veto_approver_ids": [],
                "backup_approvers": [],
                "decision_deadline_seconds": None,
                "escalation_after_seconds": None,
                "max_escalations": 0,
            }
            if self.auto_dial is not None:
                body["auto_dial"] = self.auto_dial
            return httpx.Response(200, json=body)
        if request.method == "POST" and path == "/api/v1/requests":
            body = json.loads(request.content)
            self.posts.append({"body": body, "key": request.headers.get("Idempotency-Key")})
            key = request.headers["Idempotency-Key"]
            replay = key in self.requests
            if not replay:
                self.requests[key] = {
                    "id": uuid.uuid4(),
                    "policy_id": uuid.UUID(body["policy_id"]),
                    # The real API strips these.
                    "action": body["action"].strip(),
                    "summary": body["summary"].strip(),
                    "context": body["context"],
                }
            return httpx.Response(
                200 if replay else 201, json=self.payload("pending", self.requests[key])
            )
        if request.method == "GET" and path.startswith("/api/v1/requests/"):
            step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
            if isinstance(step, httpx.Response):
                return step
            if callable(step):
                return httpx.Response(200, json=step(self.payload("pending", self.created)))
            return httpx.Response(200, json=self.payload(step, self.created))
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "nf"}})


def write_rules(tmp_path: Path, rules=None) -> Path:
    path = tmp_path / "rules.json"
    data = {
        "rules": rules
        if rules is not None
        else [
            {
                "name": "production deploy",
                "pattern": r"(^|[\s;&|(])(\./)?deploy\.(sh|ps1|cmd)\s+production\b",
                "policy_id": str(POLICY_ID),
                "action": ACTION,
                "summary": SUMMARY_TEMPLATE,
            },
            {
                "pattern": r"\bgit\s+push\b",
                "policy_id": str(POLICY_ID),
                "action": "git_push",
                "summary": "Push: {command}",
            },
        ]
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def hook_input(command=DEPLOY, *, tool_name="Bash", tool_use_id="toolu_01ABCdef", **extra):
    data = {
        "session_id": "sess-123",
        "transcript_path": "C:/Users/x/.claude/projects/t.jsonl",
        "cwd": CWD,
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": command, "description": "Deploy", "timeout": 120000},
        **extra,
    }
    if tool_use_id is not None:
        data["tool_use_id"] = tool_use_id
    return json.dumps(data).encode()


def env_for(rules_path, **extra) -> dict[str, str]:
    return {
        "APPROVAL_LINE_API_URL": "http://approval-line.test",
        "APPROVAL_LINE_API_KEY": SECRET,
        ENV_RULES: str(rules_path),
        ENV_TIMEOUT: "60",
        **extra,
    }


def run(backend, raw, env):
    result = asyncio.run(
        run_guard(raw, env, transport=httpx.MockTransport(backend.handle), clock=FakeClock())
    )
    assert SECRET not in result.stderr
    return result


@pytest.fixture
def rules(tmp_path):
    return write_rules(tmp_path)


# --- Pass-through ------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["ls -la", "git status", "./deploy.sh staging", "echo push"])
def test_non_matching_commands_pass_silently_without_network(rules, command):
    backend = FakeBackend()
    # No API settings at all: a non-matching command must not need them.
    result = run(backend, hook_input(command), {ENV_RULES: str(rules)})
    assert (result.exit_code, result.stderr) == (EXIT_ALLOW, "")
    assert backend.calls == []


@pytest.mark.parametrize("tool_name", ["Read", "Edit", "Write", "Grep", "mcp__x__y"])
def test_non_shell_tools_are_not_guarded(rules, tool_name):
    backend = FakeBackend()
    result = run(backend, hook_input(tool_name=tool_name), env_for(rules))
    assert result.exit_code == EXIT_ALLOW and backend.calls == []


# --- PowerShell (on Windows the PowerShell tool is on by default and is the primary shell) ----


def test_powershell_commands_are_guarded_like_bash(rules):
    backend = FakeBackend(["approved"])
    result = run(
        backend, hook_input("./deploy.ps1 production", tool_name="PowerShell"), env_for(rules)
    )
    assert result.exit_code == EXIT_ALLOW
    body = backend.posts[0]["body"]
    assert body["action"] == ACTION
    assert body["context"]["shell"] == "PowerShell"
    assert body["context"]["command"] == "./deploy.ps1 production"


def test_powershell_rejection_blocks(rules):
    backend = FakeBackend(["rejected"])
    result = run(backend, hook_input("git push", tool_name="PowerShell"), env_for(rules))
    assert result.exit_code == EXIT_BLOCK and "Rejected by an approver" in result.message


def test_unguarded_powershell_commands_pass_without_network(rules):
    backend = FakeBackend()
    result = run(backend, hook_input("Get-ChildItem", tool_name="PowerShell"), env_for(rules))
    assert (result.exit_code, backend.calls) == (EXIT_ALLOW, [])


def test_powershell_call_without_a_command_blocks(rules):
    raw = json.dumps({"tool_name": "PowerShell", "tool_input": {}}).encode()
    result = run(FakeBackend(), raw, env_for(rules))
    assert result.exit_code == EXIT_BLOCK and "PowerShell tool call" in result.message


# --- Approval -----------------------------------------------------------------------------------


def test_approved_matching_command_is_allowed(rules):
    backend = FakeBackend(["pending", "pending", "approved"])
    result = run(backend, hook_input(), env_for(rules))
    assert (result.exit_code, result.stderr) == (EXIT_ALLOW, "")
    (post,) = backend.posts
    assert post["key"] == "guard-toolu_01ABCdef"
    body = post["body"]
    assert (body["policy_id"], body["action"]) == (str(POLICY_ID), ACTION)
    assert body["summary"] == (f"Claude Code wants to run `{DEPLOY}` in {CWD} (production deploy).")
    assert body["context"] == {
        "command": DEPLOY,
        "working_directory": CWD,
        "guard_rule": "production deploy",
        "shell": "Bash",
        "requested_by": "Claude Code (Approval Line guard)",
    }
    # Preflight, create, then polls: the guard never schedules calls itself.
    assert not any("voice-calls" in call for call in backend.calls)
    assert backend.calls[0] == f"GET /api/v1/policies/{POLICY_ID}"


def test_the_first_matching_rule_wins(rules):
    backend = FakeBackend()
    run(backend, hook_input("git push origin main"), env_for(rules))
    assert backend.posts[0]["body"]["action"] == "git_push"


# --- Every other outcome blocks -------------------------------------------------------------


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        (["pending", "rejected"], "Rejected by an approver"),
        (["expired"], "deadline passed without approval"),
        (["pending"], "No decision before the timeout (60 s)"),
        (["condition"], "condition that is still unresolved"),
        (["reapproval"], "must re-approve"),
    ],
)
def test_unapproved_outcomes_block_with_the_request_id(rules, script, expected):
    backend = FakeBackend(script)
    result = run(backend, hook_input(), env_for(rules))
    assert result.exit_code == EXIT_BLOCK
    assert expected in result.message
    assert str(backend.created["id"]) in result.message
    assert result.message.startswith("Approval Line guard: ")
    assert result.message.endswith(STOP_INSTRUCTION)


def test_timeout_is_bounded(rules):
    backend = FakeBackend(["pending"])
    run(backend, hook_input(), env_for(rules, **{ENV_TIMEOUT: "30"}))
    polls = [c for c in backend.calls if c.startswith("GET /api/v1/requests/")]
    assert 1 < len(polls) <= 16  # 2 s doubling-ish backoff capped at 10 s over 30 s


def _drop_evaluation(payload):
    payload.pop("evaluation")
    return payload


def _authorized_while_pending(payload):
    payload["evaluation"]["authorized"] = True
    return payload


@pytest.mark.parametrize(
    "step",
    [
        _drop_evaluation,
        _authorized_while_pending,
        lambda p: {**p, "status": "approved"},  # approved status, pending evaluation
        lambda p: "not an object",
        httpx.Response(200, text="<html>"),
    ],
)
def test_malformed_or_contradictory_answers_block(rules, step):
    backend = FakeBackend([step])
    result = run(backend, hook_input(), env_for(rules))
    assert result.exit_code == EXIT_BLOCK
    assert "untrustworthy answer" in result.message


def test_backend_down_during_the_wait_blocks(rules):
    backend = FakeBackend([httpx.Response(503, json={})])
    result = run(backend, hook_input(), env_for(rules))
    assert result.exit_code == EXIT_BLOCK
    assert "Approval Line unreachable, the command was not run" in result.message


def test_backend_down_before_creation_blocks(rules):
    backend = FakeBackend()
    backend.down = True
    result = run(backend, hook_input(), env_for(rules))
    assert result.exit_code == EXIT_BLOCK
    assert "could not be created (backend_unreachable)" in result.message
    # The unreadable policy was only a warning; the failed creation is the block.
    assert "warning: could not read policy" in result.stderr


def test_refused_creation_blocks(rules):
    backend = FakeBackend()

    def refuse(request):
        if request.method == "POST":
            return httpx.Response(
                422, json={"error": {"code": "policy_inactive", "message": "inactive"}}
            )
        return FakeBackend.handle(backend, request)

    result = asyncio.run(
        run_guard(hook_input(), env_for(rules), transport=httpx.MockTransport(refuse))
    )
    assert result.exit_code == EXIT_BLOCK and "(policy_inactive)" in result.message


def test_gate_refuses_an_approval_for_a_different_action(rules):
    backend = FakeBackend(["approved"], action="rm_rf_everything")
    result = run(backend, hook_input(), env_for(rules))
    assert result.exit_code == EXIT_BLOCK
    assert "does not match this command" in result.message
    assert "approved action differs" in result.message


def test_gate_refuses_an_approval_for_a_different_command(rules):
    other = "Claude Code wants to run `./deploy.sh production --force` elsewhere."
    backend = FakeBackend(["approved"], summary=other)
    result = run(backend, hook_input(), env_for(rules))
    assert result.exit_code == EXIT_BLOCK and "approved summary differs" in result.message


# --- Configuration -----------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["APPROVAL_LINE_API_URL", "APPROVAL_LINE_API_KEY"])
def test_missing_api_settings_block_a_matching_command(rules, missing):
    backend = FakeBackend()
    env = env_for(rules)
    del env[missing]
    result = run(backend, hook_input(), env)
    assert result.exit_code == EXIT_BLOCK
    assert f"{missing} is not set" in result.message
    assert backend.calls == []


def test_missing_rules_setting_blocks(tmp_path):
    result = run(FakeBackend(), hook_input("ls"), {})
    assert result.exit_code == EXIT_BLOCK and f"{ENV_RULES} is not set" in result.message


def test_missing_rules_file_blocks(tmp_path):
    result = run(FakeBackend(), hook_input("ls"), {ENV_RULES: str(tmp_path / "nope.json")})
    assert result.exit_code == EXIT_BLOCK and "cannot be read" in result.message


GOOD_RULE = {"pattern": "x", "policy_id": str(POLICY_ID), "action": "a", "summary": "s"}


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("{not json", "not valid JSON"),
        (json.dumps({"rules": []}), "invalid"),
        (json.dumps([GOOD_RULE]), "invalid"),
        (json.dumps({"rules": [{**GOOD_RULE, "pattern": "(unclosed"}]}), "invalid regular"),
        (json.dumps({"rules": [{k: v for k, v in GOOD_RULE.items() if k != "action"}]}), "action"),
        (json.dumps({"rules": [{**GOOD_RULE, "policy_id": "not-a-uuid"}]}), "policy_id"),
        (json.dumps({"rules": [{**GOOD_RULE, "action": "x" * 201}]}), "action"),
        (json.dumps({"rules": [{**GOOD_RULE, "action": "   "}]}), "blank"),
        (json.dumps({"rules": [{**GOOD_RULE, "summary": "{env}"}]}), "unknown placeholder"),
        (json.dumps({"rules": [{**GOOD_RULE, "summary": "{command"}]}), "invalid summary"),
        (json.dumps({"rules": [{**GOOD_RULE, "polcy_id": "typo"}]}), "invalid"),
    ],
)
def test_invalid_rules_files_block_every_command(tmp_path, content, expected):
    path = tmp_path / "rules.json"
    path.write_text(content, encoding="utf-8")
    backend = FakeBackend()
    # Even a harmless command is blocked: an invalid guard is never a pass.
    result = run(backend, hook_input("ls"), env_for(path))
    assert result.exit_code == EXIT_BLOCK
    assert expected in result.message
    assert backend.calls == []


@pytest.mark.parametrize("value", ["abc", "1", "999999"])
def test_invalid_timeout_blocks(rules, value):
    result = run(FakeBackend(), hook_input(), env_for(rules, **{ENV_TIMEOUT: value}))
    assert result.exit_code == EXIT_BLOCK and ENV_TIMEOUT in result.message


@pytest.mark.parametrize(
    "raw",
    [b"", b"not json", b"[]", b'{"tool_input": {}}', json.dumps({"tool_name": "Bash"}).encode()],
)
def test_unreadable_hook_input_blocks(rules, raw):
    result = run(FakeBackend(), raw, env_for(rules))
    assert result.exit_code == EXIT_BLOCK


# --- Idempotency -------------------------------------------------------------------------------


def test_a_reinvoked_hook_replays_the_same_request(rules):
    backend = FakeBackend(["approved"])
    first = run(backend, hook_input(), env_for(rules))
    second = run(backend, hook_input(), env_for(rules))
    assert first.exit_code == second.exit_code == EXIT_ALLOW
    assert [p["key"] for p in backend.posts] == ["guard-toolu_01ABCdef"] * 2
    assert len(backend.requests) == 1  # one request, so one set of calls
    assert backend.posts[0]["body"] == backend.posts[1]["body"]  # same payload: a replay


def test_key_falls_back_to_session_and_command_without_a_tool_use_id():
    one = idempotency_key(parse_hook_input(hook_input(tool_use_id=None)))
    again = idempotency_key(parse_hook_input(hook_input(tool_use_id=None)))
    other = idempotency_key(parse_hook_input(hook_input("git push", tool_use_id=None)))
    assert one == again != other and one.startswith("guard-sc-")


def test_unusual_tool_use_ids_are_hashed_into_a_valid_key():
    key = idempotency_key(parse_hook_input(hook_input(tool_use_id="id with spaces/" + "x" * 300)))
    assert key.startswith("guard-tu-") and len(key) <= 128


# --- Auto-dial preflight -----------------------------------------------------------------------


def test_policy_with_auto_dial_off_blocks_before_creating_anything(rules):
    backend = FakeBackend(auto_dial=False)
    result = run(backend, hook_input(), env_for(rules))
    assert result.exit_code == EXIT_BLOCK
    assert "has auto-dial off, so no one would be called" in result.message
    assert backend.posts == []


def test_manual_dial_override_continues_with_a_warning(rules):
    backend = FakeBackend(["approved"], auto_dial=False)
    result = run(backend, hook_input(), env_for(rules, **{ENV_ALLOW_MANUAL_DIAL: "1"}))
    assert result.exit_code == EXIT_ALLOW
    assert "auto-dial off; continuing" in result.stderr
    assert len(backend.posts) == 1


@pytest.mark.parametrize(("policy_status", "auto_dial"), [(403, True), (404, True), (200, None)])
def test_unreadable_policy_is_only_a_warning(rules, policy_status, auto_dial):
    backend = FakeBackend(["approved"], policy_status=policy_status, auto_dial=auto_dial)
    result = run(backend, hook_input(), env_for(rules))
    assert result.exit_code == EXIT_ALLOW
    assert result.stderr.startswith("warning:")


def test_preflight_cannot_turn_a_rejection_into_a_pass(rules):
    backend = FakeBackend(["rejected"], policy_status=403)
    result = run(backend, hook_input(), env_for(rules, **{ENV_ALLOW_MANUAL_DIAL: "1"}))
    assert result.exit_code == EXIT_BLOCK and "Rejected by an approver" in result.message


# --- Bounded context, unexpected failures, the real entry point ----------------------------------


def test_long_commands_are_truncated_safely(rules):
    long_command = DEPLOY + " " + "é" * 10_000
    backend = FakeBackend(["approved"])
    result = run(backend, hook_input(long_command), env_for(rules))
    assert result.exit_code == EXIT_ALLOW
    body = backend.posts[0]["body"]
    assert len(body["context"]["command"].encode()) <= MAX_COMMAND_CONTEXT_BYTES
    assert body["context"]["command_truncated"] is True
    assert len(body["summary"]) <= 2000
    assert "more characters" in body["summary"]


def test_environment_variables_never_reach_approvers(rules):
    backend = FakeBackend(["approved"])
    run(backend, hook_input(), env_for(rules, SOME_SECRET_TOKEN="hunter2"))
    sent = json.dumps(backend.posts)
    assert "hunter2" not in sent and SECRET not in sent


def test_unexpected_exceptions_block(rules, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(guard, "build_request", explode)
    result = run(FakeBackend(), hook_input(), env_for(rules))
    assert result.exit_code == EXIT_BLOCK
    assert "unexpected error (RuntimeError)" in result.message


def _run_entry_point(raw: bytes, env: dict[str, str]) -> subprocess.CompletedProcess:
    backend_dir = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [sys.executable, "-m", "approval_line_agents.guard"],
        input=raw,
        capture_output=True,
        env={"SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "PATH": os.environ["PATH"], **env},
        cwd=backend_dir,
        timeout=60,
    )


def test_entry_point_exit_codes(rules, tmp_path):
    passed = _run_entry_point(hook_input("ls"), {ENV_RULES: str(rules)})
    assert (passed.returncode, passed.stdout, passed.stderr) == (0, b"", b"")

    blocked = _run_entry_point(hook_input("ls"), {})
    assert blocked.returncode == 2 and blocked.stdout == b""
    assert ENV_RULES.encode() in blocked.stderr

    # Backend unreachable: a closed port, real HTTP stack, still exit 2 (never 1).
    down = _run_entry_point(
        hook_input(),
        {
            ENV_RULES: str(rules),
            "APPROVAL_LINE_API_URL": "http://127.0.0.1:9",
            "APPROVAL_LINE_API_KEY": SECRET,
            "APPROVAL_LINE_TIMEOUT_SECONDS": "2",
        },
    )
    assert down.returncode == 2 and b"could not be created" in down.stderr
    assert SECRET.encode() not in down.stderr + down.stdout


# --- The shipped demo files ------------------------------------------------------------------

DEMO_TARGET = Path(__file__).resolve().parents[3] / "demo-target"


def test_example_rules_are_placeholders_and_block_until_filled_in(tmp_path):
    example = DEMO_TARGET / "guard-rules.example.json"
    result = run(FakeBackend(), hook_input("ls"), env_for(example))
    assert result.exit_code == EXIT_BLOCK and "policy_id" in result.message


def _example_rules(tmp_path):
    text = (DEMO_TARGET / "guard-rules.example.json").read_text(encoding="utf-8")
    for placeholder in ("<DEPLOY_POLICY_ID>", "<GIT_PUSH_POLICY_ID>", "<DELETE_POLICY_ID>"):
        text = text.replace(placeholder, str(POLICY_ID))
    path = tmp_path / "rules.json"
    path.write_text(text, encoding="utf-8")
    return guard.load_rules(str(path))


@pytest.mark.parametrize(
    ("tool", "command", "expected"),
    [
        ("Bash", "./deploy.sh production", "deploy_to_production"),
        ("Bash", "cd demo && ./deploy.sh production", "deploy_to_production"),
        ("Bash", "bash deploy.sh production", "deploy_to_production"),
        ("Bash", "./deploy.sh staging", None),
        ("Bash", "cat deploy.sh", None),
        ("Bash", "git push", "git_push"),
        ("Bash", "git push origin main --force", "git_push"),
        ("Bash", "git -C repo push", "git_push"),
        ("Bash", "git status && git pull", None),
        ("Bash", "echo push", None),
        ("Bash", "rm -rf build", "recursive_force_delete"),
        ("Bash", "rm -fr build", "recursive_force_delete"),
        ("Bash", "rm -r -f build", "recursive_force_delete"),
        ("Bash", "rm --recursive --force build", "recursive_force_delete"),
        ("Bash", "rm notes.txt", None),
        ("Bash", "rm -r empty_dir", None),
        ("PowerShell", "./deploy.ps1 production", "deploy_to_production"),
        ("PowerShell", r".\deploy.ps1 production", "deploy_to_production"),
        ("PowerShell", "pwsh ./deploy.ps1 production", "deploy_to_production"),
        ("PowerShell", r".\deploy.ps1 staging", None),
        ("PowerShell", "git push origin main", "git_push"),
        ("PowerShell", "Remove-Item -Recurse -Force build", "recursive_force_delete"),
        ("PowerShell", "remove-item build -recurse", "recursive_force_delete"),
        ("PowerShell", "rm -r build", "recursive_force_delete"),
        ("PowerShell", "ri build -Rec", "recursive_force_delete"),
        ("PowerShell", "Remove-Item notes.txt", None),
        ("PowerShell", "Get-ChildItem -Recurse", None),
    ],
)
def test_example_rule_patterns(tmp_path, tool, command, expected):
    rule = guard.match_rule(_example_rules(tmp_path), command, tool)
    assert (rule.rule.action if rule else None) == expected


def test_rules_can_be_limited_to_one_shell(tmp_path):
    path = write_rules(
        tmp_path,
        [
            {
                "pattern": "danger",
                "tools": ["PowerShell"],
                "policy_id": str(POLICY_ID),
                "action": "a",
                "summary": "s",
            }
        ],
    )
    rules = guard.load_rules(str(path))
    assert guard.match_rule(rules, "danger", "PowerShell") is not None
    assert guard.match_rule(rules, "danger", "Bash") is None


@pytest.mark.parametrize("tools", [[], ["Cmd"], "Bash"])
def test_invalid_rule_tools_block(tmp_path, tools):
    path = write_rules(tmp_path, [{**GOOD_RULE, "tools": tools}])
    result = run(FakeBackend(), hook_input("ls"), env_for(path))
    assert result.exit_code == EXIT_BLOCK and "tools" in result.message


def test_demo_hook_registration_blocks_on_failure_and_outlasts_the_wait(tmp_path):
    settings = json.loads((DEMO_TARGET / ".claude" / "settings.json").read_text("utf-8"))
    (entry,) = settings["hooks"]["PreToolUse"]
    (hook,) = entry["hooks"]
    # Both shells: on Windows the PowerShell tool is on by default and is the primary shell.
    assert entry["matcher"] == "Bash|PowerShell"
    assert set(entry["matcher"].split("|")) == guard.GUARDED_TOOLS
    assert hook["type"] == "command" and hook["onFailure"] == "block"
    assert hook["args"] == ["-m", "approval_line_agents.guard"]  # exec form, no shell
    # Seconds. Covers the default wait plus the preflight and creation round trips.
    assert hook["timeout"] >= guard.DEFAULT_TIMEOUT_SECONDS + 60
    mcp = json.loads((DEMO_TARGET / ".mcp.json").read_text("utf-8"))["mcpServers"]
    server = mcp["approval-line"]
    assert server["env"]["APPROVAL_LINE_API_KEY"] == "${APPROVAL_LINE_API_KEY}"  # no secret
    assert server["timeout"] > 60_000  # ms; above the wait tool's 60 s cap


def test_pre_allowed_commands_are_exact_and_always_guarded(tmp_path):
    """The allow rules only skip Claude Code's second prompt. Each names one exact command
    (no wildcard) that a guard rule matches, so the hook (which runs first and whose exit 2
    wins over allow rules) always asks a human before it runs."""
    settings = json.loads((DEMO_TARGET / ".claude" / "settings.json").read_text("utf-8"))
    allowed = settings["permissions"]["allow"]
    assert allowed and set(settings["permissions"]) == {"allow"}
    rules = _example_rules(tmp_path)
    for entry in allowed:
        tool, _, rest = entry.partition("(")
        command = rest.removesuffix(")")
        assert tool in guard.GUARDED_TOOLS and rest.endswith(")"), entry
        assert "*" not in command, entry
        assert command.endswith(("deploy.sh production", "deploy.ps1 production")), entry
        rule = guard.match_rule(rules, command, tool)
        assert rule is not None and rule.rule.action == "deploy_to_production", entry
