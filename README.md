# Approval Line

A voice-native approval system for AI agents. When an agent reaches a consequential
action (e.g. a production deploy), it submits an approval request. Approval Line applies
the organization's policy (unanimous or quorum, vetoes, backups, deadlines, conditions),
phones the approvers and collects their decisions. The agent receives a structured
result: `pending`, `approved`, `rejected` or `expired`, with an `evaluation` that says
whether the action is authorized and what happens next.

| Part | Status |
|---|---|
| [`backend/`](backend/README.md): REST API, policy engine, PostgreSQL schema | Chunk 1 ✅ |
| Bolna voice calls to approvers ([docs](backend/docs/voice-bolna.md)) | Chunk 2 ✅ one live approval call verified; other paths mock-verified |
| Quorum/veto, conditions, deadlines, escalation, audit timeline ([docs](backend/docs/policies.md)) | Chunk 3 ✅ |
| [`frontend/`](frontend/README.md) dashboard (decision timeline, call evidence), MCP server, demo agent ([docs](backend/docs/dashboard-mcp.md)) | Chunk 4 ✅ mock-verified |
| Hardening, idempotency, demo tooling, CI, deployment guide ([runbook](backend/docs/demo-runbook.md), [deploy](backend/docs/deployment.md), [security](backend/docs/security.md)) | Chunk 5 ✅ mock-verified; not deployed |
| Auto-dial: opt-in per policy, calls approvers as soon as a request is created ([docs](backend/docs/voice-bolna.md#auto-dial-opt-in-per-policy)) | ✅ mock-verified |
| Claude Code execution guard: a PreToolUse hook that blocks risky Bash and PowerShell commands until a human approves by phone ([docs](backend/docs/claude-code-guard.md), [demo](demo-target/README.md)) | ✅ mock-verified; not yet run in a live Claude Code session |
| Demo hardening: read-only preflight `scripts/demo-doctor.ps1`, tunnel start in `start-demo.ps1`, [rehearsal matrix](demo-target/REHEARSAL.md) | ✅ |
| MCP `wait_for_approval_decision` (bounded blocking wait, at most 60 s per call) ([docs](backend/docs/dashboard-mcp.md)) | ✅ mock-verified |

By default an operator starts the calls with **Schedule calls** in the dashboard. A
policy with `auto_dial` enabled has them scheduled server-side when the request is
created, so the agent's key needs no voice permission.

See [backend/README.md](backend/README.md) for setup, API reference and tests, then
[backend/docs/demo-runbook.md](backend/docs/demo-runbook.md) for the one-command local demo.
