# demo-target: Claude Code + Approval Line execution guard

A throwaway project for showing Approval Line gating a coding agent. When Claude Code,
started **in this folder**, tries to run a risky Bash command (`./deploy.sh production`,
`git push`, `rm -rf`), a `PreToolUse` hook stops it, Approval Line phones the approver,
and the command runs only if they approve.

```
Claude Code ── Bash "./deploy.sh production" ──▶ PreToolUse hook (approval_line_agents.guard)
                                                   │ rule matches
                                                   ├─▶ GET  /policies/{id}   (auto_dial on?)
                                                   ├─▶ POST /requests        (Idempotency-Key = tool-use ID)
                                                   │        └─ backend auto-dials the approver
                                                   └─▶ GET  /requests/{id}   (bounded polling)
approver answers the phone ──▶ vote ──▶ policy ──▶ approved? exit 0 (runs) : exit 2 (blocked)
```

The hook is registered only in `demo-target/.claude/settings.json`, so it is active only
when Claude Code is started from this folder. It is never active while you develop the
Approval Line repository itself. `deploy.sh` and `deploy.ps1` deploy nothing: they write
one JSON record to `output/`.

## Prerequisites

- **Claude Code v2.1.295 or later**, for the `onFailure` hook field the guard relies on.
  Git for Windows is recommended, because Claude Code's Bash tool uses Git Bash.
- **The Approval Line API**, migrated to `0006` (`alembic upgrade head` in `backend/`) and
  running on `http://127.0.0.1:8000`, plus the **voice worker**. See `backend/README.md`.
- **One policy with auto-dial on.** Tick **Auto-dial approvers** in the dashboard, or send
  `PATCH /api/v1/policies/<id>` with `{"auto_dial": true}`. Its approver needs a phone
  number: a real, verified one for a live Bolna call, or any `+141555501xx` number with
  `VOICE_PROVIDER=mock`.

## Setup (Windows PowerShell)

1. **Create an agent key for the guard** (from `backend/`). It needs no voice scope:
   ```powershell
   ..\env\Scripts\python.exe -m app.cli create-key --org-id <org-id> --name claude-code-guard --scopes "requests:create,requests:read,policies:read"
   ```
   The key is printed once. Keep it out of files and out of git.
   `policies:read` is only for the auto-dial check; without it the guard warns and goes on.

2. **Create your rules file** (from `demo-target/`):
   ```powershell
   Copy-Item guard-rules.example.json guard-rules.json
   ```
   Replace `<DEPLOY_POLICY_ID>`, `<GIT_PUSH_POLICY_ID>` and `<DELETE_POLICY_ID>` with
   policy UUIDs. They may all be the same policy. `guard-rules.json` is git-ignored.
   Until the placeholders are replaced, the file is invalid and the guard blocks
   **every** Bash command (fail closed).

3. **Set the environment in the PowerShell window you will start Claude Code from.** The
   hook and the MCP server inherit it:
   ```powershell
   $env:APPROVAL_LINE_API_URL = "http://127.0.0.1:8000"
   $env:APPROVAL_LINE_API_KEY = Read-Host "Approval Line agent key"   # paste; not saved anywhere
   $env:APPROVAL_LINE_GUARD_RULES = (Resolve-Path .\guard-rules.json).Path
   # Optional:
   $env:APPROVAL_LINE_GUARD_TIMEOUT_SECONDS = "300"      # total wait for a decision (5-3000)
   # $env:APPROVAL_LINE_GUARD_ALLOW_MANUAL_DIAL = "1"    # allow policies with auto-dial off
   ```

4. **Check the guard without calling anyone.** A harmless command must pass silently
   (exit 0) and needs no network:
   ```powershell
   '{"tool_name":"Bash","tool_input":{"command":"ls"},"session_id":"check","cwd":"."}' | ..\env\Scripts\python.exe -m approval_line_agents.guard; $LASTEXITCODE
   ```
   Don't pipe a `./deploy.sh production` input in the same way unless you mean it. That
   creates a real request, and with `VOICE_PROVIDER=bolna` it phones the approver.

5. **The MCP server (optional).** It lets Claude read the status, timeline and policy, and
   wait for a decision. `demo-target/.mcp.json` registers it at project scope. It reads the
   key from `${APPROVAL_LINE_API_KEY}`, so no secret is stored. Claude Code asks you to
   approve the project server the first time. To register it by hand instead:
   ```powershell
   claude mcp add --transport stdio --env APPROVAL_LINE_API_URL=http://127.0.0.1:8000 approval-line -- D:\voice_up\env\Scripts\python.exe -m approval_line_agents.mcp_server
   ```
   Note that `--env APPROVAL_LINE_API_KEY=...` would store the key in Claude Code's
   config file; the `.mcp.json` route avoids that.

6. **Start Claude Code here:**
   ```powershell
   cd D:\voice_up\demo-target
   claude
   ```
   Run `/hooks` and check that a `PreToolUse` hook with matcher `Bash` is listed.

## Running the demo

Ask Claude: *"Deploy to production by running ./deploy.sh production."*

- Claude proposes the command, and the status line shows *"Approval Line: waiting for a
  human to approve by phone"*.
- The approver's phone rings (or, with the mock provider, the call appears in the
  dashboard as SIMULATED).
- **On approval,** the hook exits 0. Claude Code continues its normal permission flow, so
  it may still ask you to confirm the command. The command then runs and writes
  `output/deploy-production-*.json`.
- **On rejection, a timeout, an unclear answer or any error,** the hook exits 2. Claude
  sees a plain reason with the request ID, for example *"Approval Line guard: Rejected by an
  approver. The command was not run. (approval request ...)"*.

With `VOICE_PROVIDER=mock`, answer the simulated call from `backend/`. The call ID is on the
request page in the dashboard:
```powershell
..\env\Scripts\python.exe -m app.cli voice-simulate --call-id <voice-call-id> --decision approve
```

## Limits

- The rules are regular expressions over the command text. They are a demo guard, not a
  sandbox: `bash -c "$(...)"`, aliases or other tools can evade them.
- Only the **Bash** tool is guarded. If Claude Code uses its PowerShell tool, these rules
  do not apply.
- **Retries create new requests.** The idempotency key is the tool-use ID, so re-invoking
  the hook for the same tool call never creates a second request. A *new* attempt after a
  block is a new tool call, which means a new request and a new phone call.

See `backend/docs/claude-code-guard.md` for the full contract and the failure modes, and
`DEMO_SCRIPT.md` for the presenter script.
