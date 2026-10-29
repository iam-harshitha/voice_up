# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Approval Line: human authorization for AI agents. An agent submits an approval request.
An organization policy (unanimous or quorum, veto modes, backups, deadline) decides it
from approvers' votes, and the agent reads back `pending` / `approved` / `rejected` /
`expired` plus a structured `evaluation`. Development is in chunks:

- **Chunk 1 (done):** the core approval backend in `backend/`.
- **Chunk 2 (done):** Bolna voice calls to approvers (`backend/docs/voice-bolna.md`).
  Live-verified on 2026-10-28/29 with one approver (approve, reject, uncertain, declined
  call, unconfirmed identity, unverified number); conditions, multi-approver policies and
  reconciliation are mock-verified only.
- **Chunk 3 (done):** quorum/veto, conditional approvals, deadlines, escalation to
  backups, policy audit events, audit timeline API (`backend/docs/policies.md`).
- **Chunk 4 (done, mock-verified):** React dashboard (`frontend/`) with cookie sessions +
  CSRF, MCP server and demo agent (`backend/approval_line_agents/`)
  (`backend/docs/dashboard-mcp.md`).
- **Chunk 5 (done, mock-verified):** hardening and demo readiness: `Idempotency-Key` on
  `POST /requests` (migration 0005), production fail-closed config, failure-injection
  tests, `demo-seed`, `scripts/start-demo.ps1` / `stop-demo.ps1`, CI, deployment
  artifacts (`backend/docs/deployment.md`, `security.md`, `demo-runbook.md`).

Don't build later-chunk features while working on an earlier one.

## Commands

All commands run from `backend/`. The virtualenv is at the repo root (`../env`):
`..\env\Scripts\Activate.ps1` in PowerShell, or call `../env/Scripts/python.exe`
directly. Configuration lives in `backend/.env` (git-ignored; template in `.env.example`).
Locally it uses the PostgreSQL 18 Windows service, not Docker; Docker isn't installed on
this machine.

```
pip install -e ".[dev]"
alembic upgrade head            # app never migrates on startup
alembic check                   # verify models == migrations (run after model changes)
alembic revision --autogenerate -m "..."   # then review; hand-add triggers/functions
uvicorn app.main:create_app --factory --reload
python -m app.cli create-org --name X --key "agent=requests:create,requests:read"
pytest                          # all tests; integration tests use TEST_DATABASE_URL
pytest tests/integration/test_votes.py::TestConcurrency::test_concurrent_duplicate_votes_record_once
pytest -m "not integration"     # no database needed
python -m app.workers.voice_worker --once     # voice jobs + deadline/escalation jobs
python -m app.cli voice-simulate --call-id <id> --decision approve   # mock + dev only
python -m app.cli create-dashboard-user --org-id <id> --email x@example.com --name X --scopes "..."
python -m approval_line_agents.mcp_server    # needs APPROVAL_LINE_API_URL / _API_KEY
python -m approval_line_agents.demo --policy-id <id>
python -m app.cli demo-seed --org-id <id> --yes [--issue-agent-key]   # dev only, additive
python scripts/measure_latency.py            # test DB only; never while pytest runs
# repo root: .\scripts\start-demo.ps1 -Migrate ; .\scripts\stop-demo.ps1
# repo root: .\scripts\demo-doctor.ps1    # read-only live-demo preflight (no calls, no secrets)
# frontend (from frontend/): npm ci; npm run dev | typecheck | test | build | check:bundle
ruff check . ; ruff format .
```

The test suite drops and rebuilds the `TEST_DATABASE_URL` schema through the Alembic
migrations, so the migrations are tested on every run. Never point it at the dev
database. The conftest refuses unless the database name contains `test`.

## Architecture

Layering is strict: `api/` (HTTP) → `services/` (business rules and transactions) →
`db/models/`. `domain/approval_rules.py` is a pure function with no FastAPI or
SQLAlchemy imports. It is the **only** place a decision is computed. Never accept a
decision from a caller, a voice provider or an LLM; they submit votes only.

Invariants that span several files:

- **Auth chain** (`api/dependencies.py`): `authenticate` (API key → `Principal`, 401),
  then `get_organization_context` (tenant comes from the key, never from the body),
  then `require_scope(Scope.X)` (403). Every service function takes `organization_id`
  and filters on it in the query itself.
- **Database-level tenant isolation:** child tables carry `organization_id` and use
  composite foreign keys `(parent_id, organization_id)` → `UNIQUE(id, organization_id)`
  on the parent. New tenant-owned tables should follow this pattern.
- **Policy snapshot:** `policy_snapshot` (v2; v1 still readable via
  `domain/snapshot.read_snapshot`) plus the trigger-protected `request_approvers` table
  freeze the rules at creation. Votes, conditions and escalation steps have FKs into
  `request_approvers`. Never evaluate from the live policy. Policy edits bump `version`
  and write `policy.*` audit events (with `policy_id`, not `request_id`).
- **Seats:** each primary is a seat; a backup assigned by an `escalation_steps` row may
  decide that seat. `UNIQUE(request_id, seat_approver_id)` = first valid vote wins.
- **Decision transactions** (`services/approval_service`): `record_vote`,
  `record_condition`, `resolve_condition` and `expire_if_due` all lock the request
  `FOR UPDATE`, re-evaluate with `evaluate()` and apply transitions through
  `_apply_evaluation` (status + final audit event + lapsing conditions), in one
  transaction. Errors are raised before any write. Vote check order: 404 → seat
  authorized (400) → identical repeat (200) / conflicting (409) → expired/decided (409)
  → seat decided by another (409) → open condition blocks approve (409) → a satisfied
  condition stated by another holder of the seat blocks approve (409
  `reapproval_required`) → inactive (400). Only one holder of a seat may hold
  conditions at a time (`seat_condition_pending`).
- **Conditions never approve:** an open condition, or a satisfied one whose approver has
  not re-approved, blocks approval. Resolution needs `conditions:resolve`.
- **Request jobs** (`expire_request`, `escalate_request` in `voice_jobs`) run their
  business change and their own job completion/rescheduling in ONE transaction (lock
  order: job row, then request). The worker runs them even with voice disabled.
- All call creation goes through `voice_service.create_call` (generation limits,
  duplicate-call guard), used by scheduling, escalation and re-approval. Auto-dial
  (policy `auto_dial`, default false) reuses the scheduling path via
  `approval_service.create_request`'s `on_created` hook, inside the creation
  transaction, so replays never dial; with the default, filing a request never dials.
- **Audit events:** insert-only through `services/audit_service.record_event`, in the
  caller's transaction. A DB trigger blocks UPDATE/DELETE. Order timelines by
  `sequence`, not `created_at`, because events in one transaction can share a timestamp.
- **Transactions:** services own commit/rollback through `transaction(session)`.
  `authenticate` rolls back its read transaction so service transactions start fresh.
  Sessions are pinned to UTC (`options=-c timezone=UTC`).
- **Errors:** raise `AppError` subclasses (`core/errors.py`) with a stable `code`.
  Handlers render `{"error": {code, message, request_id}}`. Unhandled exceptions become
  a generic 500 in `RequestContextMiddleware` (`core/logging.py`).
- Write schemas extend `StrictInput` (`extra="forbid"`), so clients can't send
  `status`, `organization_id`, a snapshot, and so on.

`POST /requests/{id}/votes` trusts the caller's `approver_id`; that is why `votes:write`
is never in the agent scopes. Voice votes never use it.

Voice subsystem invariants (see `backend/docs/voice-bolna.md`):

- Only `app/voice/bolna.py` knows Bolna field names; everything else uses
  `app/voice/base.py` types. `app/voice/decision.py` is pure and is the only place a
  call becomes a vote *candidate*; the policy engine still decides the request.
- Webhooks are signals only: stored in `voice_provider_events` (deduplicated) and
  acknowledged after commit. For terminal statuses the worker re-fetches the execution
  with our API key; decisions are never read from a webhook body.
- `voice_webhook_service.apply_execution` is the single transaction for lifecycle,
  decision, `approval_service.record_vote` (in a savepoint), audit events and marking
  the event processed. Lock order: request, then call.
- Never hold a DB transaction across a provider HTTP call (three phases in
  `voice_job_service.run_initiate`). Ambiguous initiation → call `unknown`, never
  re-dialled automatically; re-dial is only the explicit `redial_unanswered` action.
- Call lifecycle is monotonic (`can_transition`). A DB trigger keeps a call bound to its
  request, approver, organization and provider, and lets `provider_call_id` be set once.
- List calls in snapshot order, never by `created_at` alone (calls scheduled together
  share it); the same applies to audit events (`sequence`).
- `VOICE_PROVIDER=mock` is refused in production; never fall back to the mock.
- Test fixtures' admin key has an explicit scope list; never derive roles as "all
  scopes minus some", or new scopes leak in.

## Conventions

- Never log or return API keys, `key_hash`, contact details in audit data, or raw
  exception text.
- Use only fictional phone numbers in tests and docs (`+141555501xx`).
- When a model changes, update or add a migration, then run `alembic check` and the
  full suite. `alembic check` does NOT see changed CHECK constraints or triggers; write
  those by hand (see migration 0003's `_replace_check`) and cover them with tests.

Chunk 4 invariants (see `backend/docs/dashboard-mcp.md`):

- The browser never holds an API key. Dashboard users sign in (scrypt hashes) and get an
  HttpOnly `al_session` cookie; non-GET cookie requests need `X-CSRF-Token`. Each user is
  backed by an internal API key (plaintext discarded) so scopes and attribution reuse the
  existing chain. `X-API-Key` always takes precedence over a cookie.
- Dashboard users can never hold `votes:write`; the UI has no voting control.
- The dashboard, MCP server and demo agent never compute decisions. Show/act on
  authorization only when `status == "approved"` and `evaluation.authorized is true`.
- `approval_line_agents` talks to the API over HTTP only (never the DB). Its client parses
  strictly and raises on missing/contradictory authorization fields; tool errors are never
  successes. Request creation is retried only with the same `Idempotency-Key`
  (never without one); the server replays the original request.
- Tests build `Settings(_env_file=None, ...)`: the developer's `.env` must never change
  test behaviour. Never run two pytest processes (or the latency script) at once: they
  share the test database.
- MCP stdio: stdout is protocol only; log to stderr; never put the key in results/errors.
- The gate and bounded waiting live in `approval_line_agents/authorization.py`, shared by
  the demo agent, the MCP `wait_for_approval_decision` tool and the Claude Code guard
  (`approval_line_agents/guard.py`, `backend/docs/claude-code-guard.md`). The guard fails
  closed: every path other than an approval that passes `execution_gate` exits 2. It never
  schedules calls (auto-dial does). Its hook is registered only in `demo-target/.claude/`,
  for both shell tools (`Bash|PowerShell`: on Windows PowerShell is Claude's default
  shell). Every block message ends with `STOP_INSTRUCTION` (no retries, no workarounds).
