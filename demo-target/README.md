# demo-target: Claude Code + Approval Line execution guard

A throwaway project for showing Approval Line gating a coding agent. When Claude Code,
started **in this folder**, tries to run a risky shell command (`./deploy.sh production`,
`git push`, `rm -rf`, `Remove-Item -Recurse`), a `PreToolUse` hook stops it, Approval Line
phones the approver, and the command runs only if they approve. Both of Claude Code's shell
tools are guarded: **Bash**, and **PowerShell**, which on Windows is on by default and is
Claude's primary shell.

```
Claude Code ── Bash/PowerShell "./deploy.sh production" ──▶ PreToolUse hook (guard)
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
one JSON record to `output/`. `CLAUDE.md` in this folder tells Claude to stop and ask you
when a command is blocked, never to retry it or work around it.

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

1. **Connect this project** (once, from `backend/`, with the API running):
   ```powershell
   ..\env\Scripts\python.exe -m app.cli connect-claude --project demo-target --policy-id <policy-id>
   ```
   It checks the API and the policy (active, auto-dial on), issues a `claude-code-guard`
   key with only `requests:create,requests:read,policies:read`, and writes two
   git-ignored files (it refuses if they are not ignored):
   - `.claude/settings.local.json`: an `env` block with `APPROVAL_LINE_API_URL`,
     `APPROVAL_LINE_API_KEY` and `APPROVAL_LINE_GUARD_RULES`. Claude Code hands these to
     the hook and the MCP server, and they win over any variables in your window. So
     **no window needs anything set by hand**.
   - `guard-rules.json`: `guard-rules.example.json` with the policy ID filled in. A rule
     may add `"tools": ["PowerShell"]` (or `["Bash"]`) to apply to one shell only.

   The key is never printed. Running it again issues a new key and revokes the old one.
   It ends by running the read-only doctor; the verdict must be READY. Optional settings
   (`APPROVAL_LINE_GUARD_TIMEOUT_SECONDS`, `APPROVAL_LINE_GUARD_ALLOW_MANUAL_DIAL`) can be
   added to the same `env` block.

   *Without connect-claude:* create the key with `app.cli create-key`, copy the example
   rules and replace every `<..._POLICY_ID>` placeholder, then set the three variables in
   the window you start Claude Code from. Until the placeholders are replaced the rules
   file is invalid and the guard blocks **every** shell command (fail closed).

2. **Check the guard without calling anyone.** A harmless command must pass silently
   (exit 0) and needs no network. When you run the guard by hand there is no Claude Code
   to supply the settings, so set the rules path in this window. Windows PowerShell 5.1
   also prefixes text it pipes into a program with a UTF-8 byte-order mark, which the
   guard rejects as invalid JSON (exit 2, fail closed); the first line switches that off.
   Claude Code itself sends the hook clean JSON:
   ```powershell
   [Console]::InputEncoding = New-Object System.Text.UTF8Encoding $false
   $env:APPROVAL_LINE_GUARD_RULES = (Resolve-Path .\guard-rules.json).Path
   '{"tool_name":"Bash","tool_input":{"command":"ls"},"session_id":"check","tool_use_id":"manual-check","cwd":"."}' | ..\env\Scripts\python.exe -m approval_line_agents.guard; $LASTEXITCODE
   ```
   Don't pipe a `./deploy.sh production` input in the same way unless you mean it. That
   creates a real request, and with `VOICE_PROVIDER=bolna` it phones the approver.

3. **The MCP server (optional).** It lets Claude read the status, timeline and policy, and
   wait for a decision. `demo-target/.mcp.json` registers it at project scope and stores
   no secret: the server inherits `APPROVAL_LINE_API_URL` and `APPROVAL_LINE_API_KEY` from
   Claude Code, settings file included. Don't add `"${APPROVAL_LINE_API_KEY}"`-style
   entries to its `env`: those expand from the window's variables only, so in a window
   without them the server would get the literal text. Claude Code asks you to approve
   the project server the first time.

4. **Run the preflight** (read-only: no calls, no secrets printed). It reads
   `settings.local.json` the way Claude Code does and shows where each setting comes from:
   ```powershell
   ..\scripts\demo-doctor.ps1
   ```
   Fix every `FAIL` until the verdict is `READY`. `REHEARSAL.md` lists what to rehearse.

5. **Start Claude Code here** (any new window; nothing to set first):
   ```powershell
   cd <repo>\demo-target    # you are probably here already
   claude
   ```
   Accept the workspace trust dialog if asked: the pre-allow rule (below) only applies in a
   trusted folder. Run `/hooks` and check that a `PreToolUse` hook with matcher
   `Bash|PowerShell` is listed. Claude Code reads `settings.local.json` when it starts, so
   restart it after running `connect-claude` again.

## How the hook and the pre-allow rule interact

`.claude/settings.json` pre-allows **only** the exact demo deploy commands:
`Bash(./deploy.sh production)`, `PowerShell(./deploy.ps1 production)` and
`PowerShell(.\deploy.ps1 production)`. They contain no wildcard, so nothing else is
pre-allowed. According to the Claude Code permissions docs ("Extend permissions with hooks"):

- PreToolUse hooks run **before** the permission prompt.
- A hook that exits 2 "stops the tool call before permission rules are evaluated, so the
  block applies even when an allow rule would otherwise let the call proceed".
- Deny and ask rules are evaluated whatever a hook returns.
- Project `allow` rules apply only after you accept the folder's workspace trust dialog.

So for `./deploy.sh production`, the guard always runs first and phones the approver. A
block (exit 2) wins over the allow rule. Only after a verified approval (exit 0) does the
allow rule apply, so Claude Code runs the command **without a second on-screen prompt**.
Any other command, including a variation such as `./deploy.sh production --force`, gets
Claude Code's normal prompt after the guard.

## Running the demo

Ask Claude: *"Deploy to production by running ./deploy.sh production."*

- Claude proposes the command, and the status line shows *"Approval Line: waiting for a
  human to approve by phone"*.
- The approver's phone rings (or, with the mock provider, the call appears in the
  dashboard as SIMULATED).
- **On approval,** the hook exits 0. For the exact deploy command the pre-allow rule
  applies, so it runs without a second prompt and writes
  `output/deploy-production-*.json`. Other approved commands, such as `git push`, still get
  Claude Code's normal confirmation prompt.
- **On rejection, a timeout, an unclear answer or any error,** the hook exits 2. Claude
  sees a plain reason with the request ID, for example *"Approval Line guard: Rejected by an
  approver. The command was not run. (approval request ...)"*, followed by "STOP: report
  this reason and the request ID to the user ... Do NOT retry this command, and do NOT look
  for a workaround". Claude should then stop and ask you.

With `VOICE_PROVIDER=mock`, answer the simulated call from `backend/`. The call ID is on the
request page in the dashboard:
```powershell
..\env\Scripts\python.exe -m app.cli voice-simulate --call-id <voice-call-id> --decision approve
```

## Limits

- The rules are regular expressions over the command text. They are a demo guard, not a
  sandbox: `bash -c "$(...)"`, aliases or other tools can evade them.
- Only the **Bash** and **PowerShell** tools are guarded. File edits, MCP tools or other
  tools are not inspected, and a script that runs a risky command internally is only
  caught if its own command line matches a rule.
- **Retries create new requests.** The idempotency key is the tool-use ID, so re-invoking
  the hook for the same tool call never creates a second request. A *new* attempt after a
  block is a new tool call, which means a new request and a new phone call.

See `backend/docs/claude-code-guard.md` for the full contract and the failure modes, and
`DEMO_SCRIPT.md` for the presenter script.
