# Approval Line

Voice-native human authorization for AI agents. When an agent reaches a consequential
action (for example a production deploy), it submits an approval request. Approval Line
applies the organization's policy (unanimous or quorum, vetoes, backups, deadlines,
conditions), phones the approvers, and returns a structured, authoritative result:
`pending`, `approved`, `rejected` or `expired`, with an `evaluation` that says whether the
action is authorized and what happens next.

| Part | Status |
|---|---|
| [`backend/`](backend/README.md): REST API, policy engine, PostgreSQL schema | Chunk 1 ✅ |
| Bolna voice calls to approvers ([docs](backend/docs/voice-bolna.md)) | Chunk 2 ✅ mock-verified; live calls not yet verified |
| Quorum/veto, conditions, deadlines, escalation, audit timeline ([docs](backend/docs/policies.md)) | Chunk 3 ✅ |
| [`frontend/`](frontend/README.md) dashboard, MCP server, demo agent ([docs](backend/docs/dashboard-mcp.md)) | Chunk 4 ✅ mock-verified |
| Hardening, idempotency, demo tooling, CI, deployment guide ([runbook](backend/docs/demo-runbook.md), [deploy](backend/docs/deployment.md), [security](backend/docs/security.md)) | Chunk 5 ✅ mock-verified; not deployed |

Start with [backend/README.md](backend/README.md) for setup, then
[backend/docs/demo-runbook.md](backend/docs/demo-runbook.md) for the one-command local demo.
