# Claude Code execution guard

`approval_line_agents.guard` is a Claude Code **PreToolUse hook** for the shell tools,
**Bash** and **PowerShell**. A command that matches a rule cannot run until a human approves it through Approval Line.
Approvers are phoned by the backend (the rule's policy has `auto_dial` on). The guard
itself never schedules calls and needs no voice scope.

**Status:** tested with a fake backend and, end to end, against the real API with the mock
voice provider (`tests/agents/test_guard.py`, `tests/integration/test_guard_e2e.py`).
Live-verified on 2026-10-29 inside a Claude Code session (v2.1.312) started in
`demo-target/`, with Bolna calls: an approved deploy ran and wrote its record, and a
rejected one was blocked, after which Claude reported the reason and request ID, did not
retry and asked how to proceed. Before a live demo, run `scripts\demo-doctor.ps1`
(read-only preflight) and work through `demo-target/REHEARSAL.md`.

## Architecture

```
Claude Code (started in demo-target/)
  │ PreToolUse, matcher "Bash|PowerShell", stdin = hook JSON
  ▼
guard.py ── no rule matches ─────────────────────────────▶ exit 0 (no network)
  │ rule matches
  ├─ GET  /api/v1/policies/{policy_id}      auto_dial off? block (advisory check)
  ├─ POST /api/v1/requests                  Idempotency-Key: guard-<tool_use_id>
  │      context = {command, working_directory, guard_rule, shell, requested_by}
  │      └─ backend: policy.auto_dial → voice calls scheduled in the same transaction
  │                  → worker → Bolna → approver's phone → webhook → re-fetch → vote
  ├─ GET  /api/v1/requests/{id}             bounded polling, 2 s backoff to 10 s
  └─ execution_gate(this rule's action + this command's summary)
        passes → exit 0 (then permission rules: the exact demo deploy is pre-allowed)
        anything else → exit 2: plain reason + request ID + STOP instruction on stderr
```

The waiting and gate code (`approval_line_agents/authorization.py`) is shared with the
demo agent and the MCP tool `wait_for_approval_decision`.

## Hook contract (verified 2026-10-29)

Checked against the official docs at <https://code.claude.com/docs/en/hooks>:

| Item | Documented behaviour | What the guard does |
|---|---|---|
| Input | JSON on stdin with `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, plus `tool_name`, `tool_input` (Bash: `command`, `description`, `timeout`, `run_in_background`; the PowerShell tool has the same fields) and `tool_use_id` | Reads `tool_name`, `tool_input.command`, `tool_use_id`, `session_id` and `cwd`. Unparsable input blocks |
| Shell tools | `tools-reference`: the **PowerShell** tool is enabled automatically on Windows without Git Bash, and **on by default** with Git Bash for claude.ai and Console accounts, where "Claude treats PowerShell as the primary shell". The docs say to match `Bash\|PowerShell` in hooks that inspect shell commands | Matcher `Bash\|PowerShell`. The guard checks both tools (`GUARDED_TOOLS`), and a rule may be limited to one with `tools` |
| Allow | Exit 0. Claude Code applies any JSON output, and the action goes ahead unless that output blocks it. Stderr is not shown | Exit 0 with no output. The guard does not print `permissionDecision: "allow"`, so Claude Code's own permission rules still apply |
| Block | Exit 2: the tool call is blocked and stderr is shown to Claude as the reason. Exit 2 blocks even if JSON was printed. (A JSON `permissionDecision: "deny"` on exit 0 is the alternative) | Exit 2, with a plain-language reason that includes the request ID |
| Other exit codes, crash, timeout | Non-blocking by default: **the tool call goes ahead**. With `"onFailure": "block"` (Claude Code v2.1.295 or later), a failure or timeout blocks the call | The guard catches every exception and exits 2 itself. The hook is registered with `"onFailure": "block"` as a second line of defence |
| Timeout | `timeout` field, in **seconds**; default 600 for command hooks | `demo-target/.claude/settings.json` sets 420, which covers the default 300 s wait plus the preflight and creation round trips |
| Command form | With `args` set, exec form: no shell, and `${CLAUDE_PROJECT_DIR}` is substituted as a plain string. The project's hook config lives in `.claude/settings.json` | `"command": "${CLAUDE_PROJECT_DIR}/../env/Scripts/python.exe"`, `"args": ["-m", "approval_line_agents.guard"]` |
| Parallel hooks | Matching hooks run in parallel; `deny` takes precedence over `allow` | Only this hook is registered in `demo-target` |
| Hooks vs permission rules | `permissions`: "PreToolUse hooks run before the permission prompt". "A hook that exits with code 2 stops the tool call before permission rules are evaluated, so the block applies even when an allow rule would otherwise let the call proceed." Deny and ask rules are evaluated whatever a hook returns. Project `allow` rules apply only after the workspace trust dialog is accepted | `demo-target/.claude/settings.json` pre-allows only the exact deploy commands (`Bash(./deploy.sh production)`, `PowerShell(./deploy.ps1 production)`, `PowerShell(.\deploy.ps1 production)`), so a phone approval isn't followed by a second prompt. The guard still runs first and can block. A test checks that every pre-allowed command is matched by a guard rule |

Not verified, because the docs don't say: the stdin encoding (the guard reads UTF-8 and
blocks on anything else), and how long a stderr message may be (the guard keeps it short).

## Configuration

| Environment variable | Required | Meaning |
|---|---|---|
| `APPROVAL_LINE_API_URL`, `APPROVAL_LINE_API_KEY` | for matching commands | Agent key with `requests:create`, `requests:read`, plus `policies:read` for the auto-dial check. **No voice scope** |
| `APPROVAL_LINE_GUARD_RULES` | yes | Path of the JSON rules file |
| `APPROVAL_LINE_GUARD_TIMEOUT_SECONDS` | no | Total wait for a decision, 5 to 3000, default 300. Keep it below the hook `timeout` |
| `APPROVAL_LINE_GUARD_ALLOW_MANUAL_DIAL` | no | `1` lets a policy with auto-dial off through the preflight (an operator must then schedule the calls) |
| `APPROVAL_LINE_TIMEOUT_SECONDS` | no | Per-HTTP-request timeout of the client, default 10 |

### Rules file

```json
{
  "rules": [
    {
      "name": "production deploy",
      "pattern": "(^|[\\s;&|(])(\\./)?deploy\\.(sh|ps1|cmd)\\s+production\\b",
      "policy_id": "<policy UUID>",
      "action": "deploy_to_production",
      "summary": "Claude Code wants to deploy to production by running: {command} (in {cwd})."
    }
  ]
}
```

- **`pattern`:** a Python regular expression, searched anywhere in the shell command. The
  first matching rule wins.
- **`tools` (optional):** `["Bash"]`, `["PowerShell"]` or both (the default). Use it when
  a pattern only makes sense in one shell, e.g. PowerShell's `rm -r` (an alias of
  `Remove-Item -Recurse`) versus Bash's `rm -r`.
- **Required fields:** `policy_id` (a UUID), `action` (1 to 200 characters, not blank) and
  `summary`, a template with only `{command}`, `{cwd}` and `{rule}`. `name` is optional.
  Unknown keys are rejected.
- **Invalid files block every command.** Bad JSON, a bad regex, a missing field, a bad
  UUID, an over-long action, an unknown placeholder or an empty list is not ignored: the
  guard blocks every shell command until the file is fixed.
- **Example:** see `demo-target/guard-rules.example.json` (rules for `deploy.sh production`,
  `git push` and `rm -rf`).

### What approvers see

- **Approval request fields:** `action` comes from the rule. `summary` is the rendered
  template, with the command cut to 300 characters and the whole summary to 2000.
- **`context`:**
  - `command`: at most 4000 UTF-8 bytes, with `command_truncated: true` when cut;
  - `working_directory`: at most 500 bytes;
  - `guard_rule`;
  - `requested_by`.

  Environment variables are never sent.
- **Repeatable for one tool call:** the request contains no timestamps, so a re-invoked
  hook replays the same request.

### Idempotency

- **The key** is `guard-<tool_use_id>`. An ID that doesn't fit the key alphabet is hashed;
  without a tool-use ID, the key is a hash of the session ID and the command.
- **Re-invoked hook:** re-running the hook for the same tool call returns the original
  request (`Idempotent-Replayed`), so no second request is created and nobody is called
  again.
- **New attempt:** a new tool call, for example Claude retrying after a block, is a new
  request and a new call.

## Failure modes

Every row blocks (exit 2) unless it says allow. Messages start with
`Approval Line guard:`, include `(approval request <id>)` when a request exists, and end
with one instruction to Claude so it doesn't loop or route around the block:

> STOP: report this reason and the request ID to the user and wait for their instructions.
> Do NOT retry this command, and do NOT look for a workaround or an alternative command to
> achieve the same result.

`demo-target/CLAUDE.md` repeats the rule. A retry is a new tool call, which means a new
request and a new phone call.

| Situation | Result | Message |
|---|---|---|
| Not a shell tool (not Bash or PowerShell) | allow | (none) |
| No rule matches | allow, no network call | (none) |
| Approved, and the gate passes for this rule's action and this command's summary | allow | (none; warnings, if any, go only to the debug log) |
| Rejected | block | "Rejected by an approver. The command was not run." |
| Expired (deadline) | block | "The approval deadline passed without approval." |
| Still pending at the timeout | block | "No decision before the timeout (N s)." Adds a note if a condition or re-approval is outstanding |
| Malformed or contradictory response | block | "Approval Line returned an untrustworthy answer." |
| Backend unreachable or 5xx while waiting | block | "Approval Line unreachable, the command was not run." |
| Request creation refused or failed | block | "the approval request could not be created (code)." |
| Approval for a different action, summary, policy or request | block | "the approval does not match this command (reason)." |
| Policy has auto-dial off | block, before any request | "policy 'X' (id) has auto-dial off, so no one would be called." (allowed with the manual-dial override) |
| Policy unreadable (no `policies:read`, 404, older backend) | warning only, continue | "warning: could not read policy ...". Advisory: everything after it still fails closed |
| `APPROVAL_LINE_API_URL` or `_API_KEY` missing | block, matching commands only | "configuration error: ... is not set." |
| Rules variable or file missing, unreadable or invalid | block, **every** shell command | "... rules file ... is invalid (first problem)." |
| Invalid `APPROVAL_LINE_GUARD_TIMEOUT_SECONDS` | block | names the variable and its range |
| Unparsable hook input | block | "the hook input ... is not valid JSON." |
| Any unexpected exception or interrupt | block | "unexpected error (ExceptionType)." |
| The guard crashes or overruns the hook timeout | block (`onFailure: "block"`) | Claude Code's own failure message |

## MCP: waiting from inside the agent

`wait_for_approval_decision(request_id, max_wait_seconds)` waits at most 60 s per call
(default 30), using the same polling code. It returns the authoritative status as soon as
the request is approved, rejected or expired. Otherwise it returns the current pending
state, and the agent calls it again. Errors (untrustworthy or unreachable backend) are tool
errors, never results. `get_approval_policy` now also returns `auto_dial`. See
`dashboard-mcp.md`.

The guard and the MCP tools are independent. With the guard in place, Claude doesn't need
to call `request_approval` before a guarded command; doing both creates two requests.

## Security notes

- **The guard computes no decision.** It allows only when the backend reports
  `status == "approved"` and `authorized == true`, and the execution gate passes. The gate
  checks the request ID, the action, the summary (which contains this command), the policy,
  the final evaluation, the decision time and the deadline.
- **Least privilege:**
  - **The guard's key:** `requests:create`, `requests:read`, and optionally `policies:read`.
    It can't vote, resolve conditions or place calls. Calls are placed server-side by the
    policy's auto-dial.
- **No secrets in output:**
  - The key is never printed. Block messages contain only codes, the policy name and ID,
    and the request ID.
  - The command and working directory go to approvers in `context` (and the voice agent may
    read them aloud); avoid putting secrets on command lines.
- **Not a sandbox:**
  - Regex rules over the command text can be evaded (`bash -c "$(…)"`, aliases, scripts
    that call the risky command internally, or tools other than Bash and PowerShell, such
    as file edits).
  - Use it as a human checkpoint for well-known risky commands, alongside Claude Code's
    permission rules, not instead of them.
- **Scope:** the hook is project-scoped (`demo-target/.claude/settings.json`). It doesn't
  affect Claude Code sessions in other folders, including this repository.
- **Fail closed:** missing or invalid configuration blocks guarded work. An invalid rules
  file blocks every shell command in that project.
