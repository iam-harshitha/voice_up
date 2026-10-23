# Approval Line

A voice-native approval system for AI agents. When an agent reaches a consequential
action (e.g. a production deploy), it submits an approval request. Approval Line applies
the organization's one- or two-person policy and collects the approvers' decisions.
The agent receives a structured result: approved, rejected or still pending.

| Part | Status |
|---|---|
| [`backend/`](backend/README.md): REST API, policy engine, PostgreSQL schema | Chunk 1 ✅ |
| Bolna voice calls to approvers | planned |
| Dashboard (decision timeline, call evidence) | planned |
| MCP / agent integration | planned |

See [backend/README.md](backend/README.md) for setup, API reference and tests.
