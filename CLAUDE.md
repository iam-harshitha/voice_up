# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Approval Line: human authorization for AI agents. An agent submits an approval request.
A one- or two-person policy decides it from approvers' votes, and the agent reads back
`pending` / `approved` / `rejected`. Development is in chunks:

- **Chunk 1 (done):** the core approval backend in `backend/`.
- **Chunk 2 (done, mock-verified only):** Bolna voice calls to approvers
  (`backend/docs/voice-bolna.md`). No live Bolna call has been tested yet.
- **Later:** a React dashboard with a decision timeline and call evidence, and MCP.

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
python -m app.workers.voice_worker --once     # process voice jobs (needs VOICE_PROVIDER)
python -m app.cli voice-simulate --call-id <id> --decision approve   # mock + dev only
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
- **Policy snapshot:** `approval_requests.policy_snapshot` freezes `required_approvals`
  and `approver_ids` at creation. Vote validation uses the snapshot, never the live
  policy. Policy edits bump `version`.
- **Vote transaction** (`services/approval_service.cast_vote`): lock the request
  `FOR UPDATE`, insert the vote, record audit events, evaluate, and update the status,
  all in one transaction via `db/session.transaction()`. Order of checks: 404 →
  approver in snapshot (400) → identical repeat returns the existing result (200) /
  conflicting vote (409) → already decided (409) → approver inactive (400).
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
  full suite.
