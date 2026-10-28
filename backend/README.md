# Approval Line — backend

Approval Line lets an AI agent ask humans for authorization before it takes a
consequential action. An organization configures a policy that needs one or two
approvers. The agent submits a request. Approvers' votes are recorded, and a
deterministic policy engine decides `pending` → `approved` / `rejected`. The agent reads
the result over a REST API.

Chunk 2 adds voice approvals: Approval Line calls each approver through Bolna, and
turns a verified, explicit spoken decision into a vote through the same approval
service. See **[docs/voice-bolna.md](docs/voice-bolna.md)** for the design, the verified
Bolna contract, setup and limitations.

Chunk 3 adds quorum and veto policies, backup approvers with durable time-based
escalation, decision deadlines, conditional approvals that can never authorize by
themselves, policy audit events, and an audit timeline API. The exact semantics,
states, endpoints and a walkthrough are in **[docs/policies.md](docs/policies.md)**.

Chunk 5 hardens and packages it: idempotent request creation, production fail-closed
configuration, failure-injection tests, a safe `demo-seed` command, one-command local
startup (`../scripts/start-demo.ps1`), CI, deployment artifacts and measured latency. See
**[docs/demo-runbook.md](docs/demo-runbook.md)**, **[docs/deployment.md](docs/deployment.md)**,
**[docs/security.md](docs/security.md)**, **[docs/performance.md](docs/performance.md)** and
**[docs/demo-pitch.md](docs/demo-pitch.md)**.

Chunk 4 adds the operator dashboard (`../frontend`, with browser sign-in via HttpOnly
session cookies and CSRF protection), an MCP server that exposes approval tools to
agents, and a demo agent that only performs a simulated deployment after an explicit,
matching authorization. See **[docs/dashboard-mcp.md](docs/dashboard-mcp.md)**, which
also has the full end-to-end demo procedure.

## Requirements

- Python 3.12+
- PostgreSQL 14+ (tested on 18). Use either a local install or Docker Compose (below).

## Setup

All commands run from `backend/`. The examples use PowerShell; on bash, activate with
`source ../env/bin/activate`.

```powershell
python -m venv ..\env                 # once (the repo uses ../env)
..\env\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env           # then edit .env, see below
```

### Database, option A: an existing local PostgreSQL

As a superuser (`psql -U postgres -h localhost`):

```sql
CREATE ROLE approval_line LOGIN PASSWORD '<choose a password>';
CREATE DATABASE approval_line OWNER approval_line;
CREATE DATABASE approval_line_test OWNER approval_line;
```

Put that password into `DATABASE_URL` and `TEST_DATABASE_URL` in `.env`. URL-encode
`@ : / %` in the password.

### Database, option B: Docker Compose

Set `POSTGRES_PASSWORD` (and the matching password in both URLs) in `.env`, then:

```powershell
docker compose up -d db               # PostgreSQL on 127.0.0.1:${POSTGRES_PORT}
python scripts/wait_for_db.py         # polls until ready (no fixed sleeps)
```

The `approval_line_test` database is created automatically the first time the volume is
initialised.

### Migrations

The application never creates or alters tables on startup. Apply migrations explicitly:

```powershell
alembic upgrade head                  # create / upgrade the schema
alembic current                       # show the applied revision
alembic check                         # fail if models and migrations have drifted
alembic downgrade base                # drop everything (development only)
```

## Run the API

```powershell
uvicorn app.main:create_app --factory --reload
```

- Liveness: `GET http://127.0.0.1:8000/api/v1/health/live`
- Readiness (checks the DB): `GET http://127.0.0.1:8000/api/v1/health/ready`
- Interactive docs: `http://127.0.0.1:8000/docs` (disabled when `APP_ENV=production`)

To run both the API and PostgreSQL in Docker:

```powershell
docker compose --profile app up -d --build
docker compose --profile app run --rm api alembic upgrade head
```

## API keys

Keys are created with the CLI. The plaintext is printed **once**; only its SHA-256 hash
is stored. There are no default keys.

```powershell
python -m app.cli create-org --name "Acme Robotics" `
  --key "deploy-agent=requests:create,requests:read" `
  --key "admin=policies:read,policies:write,approvers:read,approvers:write,requests:read" `
  --key "trusted-voter=votes:write"

python -m app.cli create-key --org-id <org-uuid> --name ci-agent --scopes requests:create,requests:read
python -m app.cli list-keys  --org-id <org-uuid>       # never shows secrets
python -m app.cli revoke-key --key-id <key-uuid>
```

Example output (redacted):

```
Organization 'Acme Robotics' created (id 1e1cb54b-9aee-4a90-90db-d37af8f98e05)
API key 'deploy-agent' (id 8da723c2-f841-4e5a-80ba-1461e27218da)
  scopes: requests:create, requests:read
  key:    al_****REDACTED****
...
Store these keys now (e.g. in your local, git-ignored .env). They are not stored and cannot be shown again.
```

Save a key straight into your shell session rather than a tracked file, e.g.
`$env:AGENT_KEY = "al_..."`. Alternatively, add a line to `.env`, which is git-ignored.
Send it as the `X-API-Key` header.

### Scopes

| Scope | Allows |
|---|---|
| `approvers:read` / `approvers:write` | list/get, create/update approvers |
| `policies:read` / `policies:write` | list/get, create/update policies |
| `requests:create` | submit an approval request |
| `requests:read` | list/get requests and their decision |
| `votes:write` | record a vote (trusted integration only, see below) |
| `voice:calls:create` | schedule approval phone calls for a request |
| `voice:calls:read` | read a request's call status and outcomes |
| `conditions:resolve` | resolve a conditional approval (never approves by itself) |
| `audit:read` | read request and policy audit timelines |

An agent key should have only `requests:create,requests:read`. It must never hold
`votes:write` or `policies:write`.

## Endpoints (`/api/v1`)

| Method & path | Scope | Notes |
|---|---|---|
| `GET /health/live` | public | process is up |
| `GET /health/ready` | public | 200 if DB reachable, else 503 |
| `POST /approvers` | approvers:write | `name` + `phone_number` (E.164) and/or `email` |
| `GET /approvers`, `GET /approvers/{id}` | approvers:read | paginated |
| `PATCH /approvers/{id}` | approvers:write | incl. `is_active` |
| `POST /policies` | policies:write | `required_approvals` 1 or 2 + exactly that many distinct active approvers |
| `GET /policies`, `GET /policies/{id}` | policies:read | `?is_active=` filter |
| `PATCH /policies/{id}` | policies:write | any change bumps `version` |
| `POST /requests` | requests:create | `policy_id`, `action`, `summary`, `context` (object, ≤16 KB); optional `Idempotency-Key` header (8–128 chars): a retry returns the original (200, `Idempotent-Replayed: true`), the same key with a different payload returns 422 `idempotency_key_reused` |
| `GET /requests`, `GET /requests/{id}` | requests:read | `?status=pending\|approved\|rejected\|expired`, `?include_evaluation=true`; detail includes `evaluation`, `expires_at`, snapshot `policy` and named `approvers` |
| `GET /requests/stats` | requests:read | exact counts by status, plus engine-derived condition/re-approval/deadline counts (bounded; see `evaluation_complete`) |
| `GET /audit-events/recent` | audit:read | newest organization events, `?limit≤50` |
| `POST /auth/login`, `GET /auth/session`, `POST /auth/logout` | dashboard credentials | browser sign-in (HttpOnly cookie + `X-CSRF-Token`); see docs/dashboard-mcp.md |
| `GET /requests/{id}/conditions`, `GET /requests/{id}/escalations` | requests:read | conditions and escalation steps |
| `POST /requests/{id}/conditions` | votes:write | record a conditional approval (trusted integration) |
| `POST /requests/{id}/conditions/{cid}/resolve` | conditions:resolve | `satisfied` \| `not_satisfied`, `note`, optional `evidence_reference` |
| `GET /requests/{id}/audit-events`, `GET /policies/{id}/audit-events` | audit:read | `?after=<sequence>&limit≤200` |
| `POST /requests/{id}/votes` | votes:write | `approver_id`, `vote` = approve\|reject; 201 new, 200 identical repeat |
| `POST /requests/{id}/voice-calls` | voice:calls:create | one call per snapshotted approver; 201 new, 200 already scheduled; optional `{"redial_unanswered": true}` |
| `GET /requests/{id}/voice-calls` | voice:calls:read | call status, attempts, decision status, outcome (no phone numbers) |
| `POST /voice/webhooks/bolna` | `X-Approval-Line-Webhook-Token` header | Bolna deliveries; not API-key authenticated |

List endpoints take `limit` (1–100, default 50) and `offset` (0–10000). Results are
ordered newest first, with ties broken by ID.

Errors always look like
`{"error": {"code": "...", "message": "...", "request_id": "..."}}`. The status codes
are: 400 business rule, 401 missing/invalid/revoked key, 403 missing scope, 404 not
found in your organization, 409 conflicting or late vote, 422 schema validation,
500 unexpected, 503 DB unavailable.

### Decision rules

- Any valid reject → `rejected`.
- Every required approver approved → `approved`.
- Otherwise → `pending`. A missing vote, an unanswered call or a timeout never approves.
- Votes count only from approvers in the request's **policy snapshot**, and only while
  the approver is still active.
- Final decisions are immutable. Any new vote on a decided request returns 409. An
  identical repeat of an existing vote returns the existing result without recording
  anything.

> **Trust boundary:** `POST /requests/{id}/votes` trusts the caller's `approver_id`.
> The approver is not independently authenticated. Only give `votes:write` to trusted
> operators and test harnesses. Voice votes do not use this endpoint: they come from
> the authenticated webhook flow and the persisted call-to-approver mapping (see
> docs/voice-bolna.md).

## Voice worker

Calls are placed, provider events processed, deadlines enforced and escalation rounds
run by a separate worker process. It uses a PostgreSQL job queue; run as many workers
as you like. With `VOICE_PROVIDER=disabled` it still runs deadline and escalation jobs.
After a restart, unfinished jobs are picked up again when their lease expires.

```powershell
python -m app.workers.voice_worker            # long-running; Ctrl+C stops after the current job
python -m app.workers.voice_worker --once     # drain due jobs and exit
```

Voice is off by default (`VOICE_PROVIDER=disabled`). To try the whole flow locally
without Bolna credentials, use the mock provider and `python -m app.cli voice-simulate`,
as described in [docs/voice-bolna.md](docs/voice-bolna.md#local-development-without-credentials).

## Manual end-to-end check

PowerShell, with the server running and keys from the CLI:

```powershell
$api = "http://127.0.0.1:8000/api/v1"
$admin = @{ "X-API-Key" = $env:ADMIN_KEY }
$agent = @{ "X-API-Key" = $env:AGENT_KEY }
$voter = @{ "X-API-Key" = $env:VOTER_KEY }
function Post($path, $headers, $body) {
  Invoke-RestMethod -Method Post -Uri "$api$path" -Headers $headers `
    -ContentType "application/json" -Body ($body | ConvertTo-Json -Depth 5)
}

$alice  = Post "/approvers" $admin @{ name = "Alice"; phone_number = "+14155550101" }
$bob    = Post "/approvers" $admin @{ name = "Bob"; email = "bob@example.com" }
$policy = Post "/policies"  $admin @{ name = "Production deploys"; required_approvals = 2;
                                      approver_ids = @($alice.id, $bob.id) }
$req    = Post "/requests"  $agent @{ policy_id = $policy.id; action = "deploy.production";
                                      summary = "Deploy payments-api v2.3.0" }

(Post "/requests/$($req.id)/votes" $voter @{ approver_id = $alice.id; vote = "approve" }).request.status  # pending
(Post "/requests/$($req.id)/votes" $voter @{ approver_id = $bob.id;   vote = "reject"  }).request.status  # rejected
Invoke-RestMethod -Uri "$api/requests/$($req.id)" -Headers $agent       # final decision + votes
```

## Tests and lint

```powershell
pytest                                  # everything (integration needs TEST_DATABASE_URL)
pytest tests/unit                       # pure policy engine, no DB
pytest -m "not integration"             # everything that needs no DB
pytest tests/integration/test_votes.py::TestConcurrency   # one class
pytest -k conflicting_duplicate         # by name
pytest tests/agents                     # MCP server, REST client, demo-agent gate (no DB)
pytest tests/integration/test_chunk4_e2e.py   # mock-backed end-to-end (DB)
ruff check . ; ruff format --check .
```

Frontend checks run from `../frontend`: `npm run typecheck`, `npm test`, `npm run build`,
`npm run check:bundle`.

Integration tests **drop and rebuild the schema** of `TEST_DATABASE_URL` using the
Alembic migrations. They refuse to run unless that database's name contains `test` and
differs from `DATABASE_URL`. Without `TEST_DATABASE_URL`, those tests are skipped and
reported as such.

## Layout

```
app/domain/approval_rules.py  pure decision engine (no FastAPI/SQLAlchemy)
app/services/                 business operations and transaction boundaries
app/api/                      FastAPI routes; dependencies.py has auth/tenant/scope layers
app/db/models/                SQLAlchemy models; migrations/ holds the Alembic history
app/core/                     settings, API-key crypto, error format, JSON logging
app/cli.py                    org / API-key / dashboard-user bootstrap, demo-seed, voice-simulate
scripts/measure_latency.py    bounded latency measurements (test database only)
approval_line_agents/         MCP server, strict REST client, demo agent (talk to the API over HTTP)
../frontend/                  React dashboard
```

## Design notes and assumptions

- **Tenant isolation is enforced by the database too.** Child tables reference their
  parents through composite `(id, organization_id)` foreign keys. A policy, vote or
  audit event can't point at another organization's rows even if a query is wrong.
  For this reason `approval_votes` carries `organization_id` as well.
- **Snapshot.** Each request stores
  `policy_snapshot = {policy_id, name, version, required_approvals, approver_ids}`.
  Votes are validated against it, never against the live policy.
- **Concurrency.** Voting locks the request row (`SELECT … FOR UPDATE`).
  `UNIQUE(request_id, approver_id)` is a second safeguard. Creating a request takes
  `FOR SHARE` on the policy, so its snapshot can't mix two policy versions.
- **Audit trail.** Events are written in the same transaction as the change they
  describe. A trigger rejects `UPDATE`/`DELETE` on `audit_events`. A `sequence`
  identity column gives a strict order, because events in one transaction can share a
  timestamp.
- **Approver deactivation.** An inactive approver can't vote, even on older requests,
  so such a request stays pending rather than approving. A policy with an inactive
  approver refuses new requests until it's updated.
- Database sessions use UTC, so timestamps are always returned with a `Z` suffix.
- Policies and organizations are never deleted, only deactivated. All foreign keys are
  `RESTRICT`.

## Remaining work

- Run the live Bolna verification checklist in docs/voice-bolna.md (no real call has
  been placed yet).
- Stronger approver verification for voice (e.g. a spoken PIN) before production use.
- Separate migration-owner and runtime database roles, so the runtime role can only
  `INSERT`/`SELECT` audit events.
- Key rotation metadata (last used, expiry) on `api_keys`.
- Per-IP sign-in rate limiting, MFA/SSO, idle session timeout for the dashboard.
