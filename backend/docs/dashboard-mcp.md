# Dashboard, MCP server and demo agent (Chunk 4)

Chunk 4 adds three interfaces on top of the Chunk 1–3 backend:
- a **React dashboard** (`frontend/`);
- an **MCP server** (`backend/approval_line_agents/mcp_server.py`);
- a **demo agent** (`backend/approval_line_agents/demo.py`).

All three use the same FastAPI application, the same PostgreSQL state and the same policy engine. None of them computes a decision. The backend's `evaluation` is the only authority, and only `status: "approved"` together with `evaluation.authorized: true` permits an action.

```
 AI agent ──MCP (stdio)──> MCP server ──REST + X-API-Key──┐
 Demo agent ──MCP or REST──────────────────────────────────┤
 Browser ──same-origin /api, session cookie + CSRF─────────┼─> FastAPI ─> services ─> engine ─> PostgreSQL
 Bolna / mock ──verified webhook──> worker ────────────────┘
```

## 1. Gap analysis: what each interface needs, and what was added

Most operations already existed. Chunk 4 adds the smallest set of new endpoints and fields.

| Operation (dashboard / MCP / agent) | Endpoint | Scope | Status before Chunk 4 | Change |
|---|---|---|---|---|
| List / get / create / update policies | `GET/POST/PATCH /policies[/{id}]` | `policies:read` / `policies:write` | existed | none |
| Policy change history | `GET /policies/{id}/audit-events` | `audit:read` | existed | adds `request_id` and `policy_id` to events |
| List / create / update / deactivate approvers | `GET/POST/PATCH /approvers[/{id}]` | `approvers:read` / `approvers:write` | existed | none |
| Create request | `POST /requests` | `requests:create` | existed | none |
| Request list with evaluation | `GET /requests?include_evaluation=true` | `requests:read` | list had no evaluation | **new opt-in query parameter** (avoids N+1 calls from the browser) |
| Request detail, including snapshot rules and named seats | `GET /requests/{id}` | `requests:read` | detail lacked snapshot rules and approver names | **new fields `policy` and `approvers`**, from the immutable snapshot, with no contact details |
| Overview counts | `GET /requests/stats` | `requests:read` | missing | **new endpoint** |
| Recent activity feed | `GET /audit-events/recent?limit≤50` | `audit:read` | missing | **new endpoint** |
| Conditions, escalations, request timeline | `GET /requests/{id}/conditions`, `/escalations`, `/audit-events` | `requests:read` / `audit:read` | existed | none |
| Resolve condition | `POST /requests/{id}/conditions/{cid}/resolve` | `conditions:resolve` | existed | none |
| Voice calls: schedule / status | `POST/GET /requests/{id}/voice-calls` | `voice:calls:create` / `voice:calls:read` | existed (idempotent; 200 if already scheduled) | none |
| Browser sign-in | `POST /auth/login`, `GET /auth/session`, `POST /auth/logout` | none (credentials) | missing | **new**, see §2 |

**Not added, on purpose:**
- **No dashboard voting UI.** Dashboard users can never hold `votes:write`.
- **No generic HTTP tool in MCP.**
- **No request search.** The API filters by status only, and the UI says so.
- **No idempotency key for request creation.** See §6.

**Migration 0004** adds `dashboard_users` and `dashboard_sessions`. Nothing else in the schema changed.

### New response contracts
- **`GET /requests/{id}`** adds:
  - `policy`: `{schema_version, policy_id, name, version, approval_mode, required_approvals, veto_mode, deadline_seconds, escalation_after_seconds, max_escalations}`.
  - `approvers`: `[{approver_id, name, role: primary|backup, can_veto, position}]`.
- **`GET /requests?include_evaluation=true`**: each item carries `evaluation`, the same object as in the detail. Without the parameter, `evaluation` is `null` and the response is unchanged.
- **`GET /requests/stats`** returns:
  - `{total, by_status: {pending, approved, rejected, expired}}`;
  - `pending_waiting_for_condition`, `pending_awaiting_reapproval` and `pending_deadline_passed`;
  - `pending_evaluated`, `pending_evaluation_limit` and `evaluation_complete`.

  The status counts are exact database aggregates. The three evaluation-derived counts come from running the real engine over the newest pending requests, capped at 200. `evaluation_complete` says whether every pending request was included, and the dashboard shows the cap when it applies.
- **`GET /audit-events/recent`** returns `{items: [AuditEvent], limit}`, newest first.
- **`AuditEvent`** gains `request_id` and `policy_id`.
- **`evaluation`** gains `reapproval_seat_ids`, and `reapproval_approver_ids` now names who must re-approve (Chunk 3 review fix).

## 2. Dashboard authentication

The backend's API keys must never reach a browser. The dashboard therefore has its own sign-in:

- **Users:** `dashboard_users` stores an email and a scrypt password hash (`app/core/passwords.py`, Python standard library `hashlib.scrypt`, n=2^15, r=8, p=1).
  - Each user is **backed by an internal API key** whose plaintext is generated and immediately discarded. That key carries the user's scopes and organization.
  - As a result, every existing scope check, tenant filter and actor attribution works unchanged. For example, `resolved_by_api_key_id` and a policy's `actor_api_key_id` identify the dashboard user's key.
  - Revoking that key, or running `disable-dashboard-user`, ends all sessions immediately.
- **Sessions:** a 256-bit random token sent in a cookie with these settings:
  - name `al_session`;
  - `HttpOnly`, `SameSite=Strict`, `Path=/api/v1`;
  - `Secure` by default in production (`DASHBOARD_COOKIE_SECURE=false` is refused there).

  Only its SHA-256 hash is stored. Sessions have an absolute expiry (`DASHBOARD_SESSION_TTL_SECONDS`, default 8 h) and are revoked on logout.
- **CSRF:** every non-GET request made with the cookie must send `X-CSRF-Token`. The token is `sha256("approval-line-csrf:" + session token)`, so it cannot be computed without the cookie. It is returned by `/auth/login` and `/auth/session` and kept in memory only. The cookie is also `SameSite=Strict`.
- **Lockout:** after `DASHBOARD_MAX_FAILED_LOGINS` (default 5) consecutive failures, the account is locked for `DASHBOARD_LOCKOUT_SECONDS` (default 900). The lock is stored in the database. Unknown emails, wrong passwords, inactive users and locked accounts all get the same `401 login_failed`, and an unknown email still costs one hash computation.
- **Precedence:** a request with `X-API-Key` is authenticated by the key and ignores any cookie. Agents are unaffected.
- **`votes:write` cannot be granted** to a dashboard user (the CLI and service refuse it), so no dashboard user can impersonate an approver.
- **Same origin:** the dashboard calls `/api/v1` on its own origin.
  - In development, Vite proxies `/api` to the API (`APPROVAL_LINE_BACKEND_URL`, default `http://127.0.0.1:8000`).
  - In production, serve `frontend/dist` and the API behind one origin (reverse proxy).
  - CORS stays credential-less; cross-origin dashboards are not supported.

Create a user. The password is prompted for, or read from the env var you name, and is never passed as an argument:

```powershell
python -m app.cli create-dashboard-user --org-id <org uuid> --email ops@example.com --name "Ops Lead" `
  --scopes "requests:read,requests:create,policies:read,policies:write,approvers:read,approvers:write,audit:read,conditions:resolve,voice:calls:create,voice:calls:read"
python -m app.cli disable-dashboard-user --email ops@example.com
```

## 3. The dashboard

See [`frontend/README.md`](../../frontend/README.md) for structure and commands.

**Pages:**
- Overview
- Approval requests (filters and server-side pagination)
- Request detail
- New request
- Policies (list, create, edit, activate/deactivate, change history)
- Approvers (list, create, edit, deactivate; contact details masked by default)

**On the request detail page:**
- **Authorization banner:** shows `✓ Authorized` only when `status === "approved"` and `evaluation.authorized === true`. Everything else is `✕ Not authorized`, including contradictory data.
- **Evaluation:** state, reason, `next_action`, counts, deadline, waiting-on, open conditions and re-approvals, all rendered from the backend evaluation.
- **Seats:** each seat with its recorded vote. A vote cast by a backup is labelled.
- **Snapshot policy and escalation steps.**
- **Conditions:** resolving one needs `conditions:resolve`, a note and a confirmation dialog. The success toast says the request is still not authorized.
- **Voice calls:** the real call records. The `mock` provider is labelled **SIMULATED** everywhere. "Schedule calls" uses the idempotent endpoint.
- **Audit timeline:** ordered by `sequence`; unknown event types are shown generically.

**Freshness:** polling.
- Request detail refreshes every 4 s while the request is pending and stops once it is final. Overview and the list refresh every 10 s.
- Requests never overlap, polling pauses while the tab is hidden, and in-flight requests are cancelled on navigation.
- After a failed refresh, the last data stays on screen with a "may be out of date" notice.

## 4. MCP server

- **SDK:** `mcp` 2.3.0 (`mcp.server.mcpserver.MCPServer`), installed with `pip install -e ".[dev]"` or `".[agents]"`.
- **Transport:** stdio. stdout carries only the protocol; logs go to stderr.
- **Adapter only:** a thin layer over the REST API, using one API key from its own environment. It never touches the database.

```powershell
$env:APPROVAL_LINE_API_URL = "http://127.0.0.1:8000"
$env:APPROVAL_LINE_API_KEY = "<agent key>"     # requests:create,requests:read (+ audit:read, policies:read)
python -m approval_line_agents.mcp_server       # or: approval-line-mcp
```

MCP client configuration (placeholders; keep the real key in the client's secret store):

```json
{
  "mcpServers": {
    "approval-line": {
      "command": "D:/voice_up/env/Scripts/python.exe",
      "args": ["-m", "approval_line_agents.mcp_server"],
      "cwd": "D:/voice_up/backend",
      "env": { "APPROVAL_LINE_API_URL": "http://127.0.0.1:8000", "APPROVAL_LINE_API_KEY": "<agent key>" }
    }
  }
}
```

| Tool | Input (validated) | Result (structured) | Scope |
|---|---|---|---|
| `request_approval` | `policy_id` (UUID), `action` (1–200), `summary` (1–2000), `context` (object, optional) | `ApprovalStatusResult` (initial state; normally `pending`) | `requests:create` (+ `requests:read`) |
| `get_approval_status` | `request_id` (UUID) | `ApprovalStatusResult` | `requests:read` |
| `list_approval_requests` | `status?` (pending/approved/rejected/expired), `limit` 1–50, `offset` 0–10000 | `{items, total, limit, offset}` | `requests:read` |
| `get_approval_timeline` | `request_id`, `after` ≥ 0, `limit` 1–200 | `{request_id, items, next_after}` | `audit:read` |
| `get_approval_policy` | `policy_id` | policy rules (approver names only) | `policies:read` |

`ApprovalStatusResult` contains:
- `request_id`, `action`, `summary`, `policy_id`, `policy_version`;
- `status` and `authorized`;
- `state`, `reason`, `next_action`;
- `approvals`, `rejections`, `required_approvals`;
- outstanding, condition and re-approval approver IDs;
- `deadline_passed`, `expires_at`, `decided_at`, `created_at`;
- `guidance`, a plain-language instruction.

**How the tools fail:**
- **Strict parsing:** responses are parsed strictly. A missing or non-boolean `authorized`, or contradictory fields (for example `authorized: true` with any status other than `approved`), produce an `untrusted_response` tool error. They are never returned as a result.
- **Unknown state:** network errors, timeouts and 5xx produce `backend_unavailable … the authorization state is unknown; do not act`.
- **Refusals:** 401, 403, 404, 409 and 422 map to `authentication_failed`, `permission_denied`, `not_found`, `conflict` and `invalid_input`.
- **No leaks:** tool errors never contain the API key or stack traces.

There are no tools to vote, resolve conditions, change status or make arbitrary HTTP calls.

## 5. Demo agent

`python -m approval_line_agents.demo --policy-id <uuid> [--transport mcp|rest] [--resume <request id>] [--timeout 600] [--poll-interval 2]`

It reads `APPROVAL_LINE_API_URL` and `APPROVAL_LINE_API_KEY` (an agent key). With `--transport mcp`, the default, it launches the MCP server over stdio and calls its tools.

1. It proposes `deploy_to_production` ("Deploy release v2.4 to production. Automated tests passed, but the release includes a database migration. Verify the backup and migration safety before proceeding.") with context: release, tests passed, migration included, backup **not** verified, simulated target.
2. It creates **one** request. Creation is never retried, because the API has no idempotency key.
3. It polls with backoff (2 s, growing ×1.5, capped at 15 s) up to `--timeout`. It stops immediately on `rejected` or `expired`, and after 3 consecutive "backend unavailable" errors.
4. **Execution gate.** The deployment runs only if all of these hold:
   - the request ID matches the created request;
   - action, summary and policy match what was proposed;
   - `status == "approved"`;
   - `authorized is True` (a strict boolean);
   - `state == "approved"` and `reason == "quorum_met"`;
   - `next_action == "none"`;
   - the deadline has not passed;
   - `decided_at` is set.

   The gate is a second check, not a policy engine.
5. **Simulated deployment:** writes `.demo_runs/deploy-<request id>.json` (`"simulated": true`, release, the authorizing request ID, policy version, timestamps). Nothing else is touched. The file is created exclusively, so one request can authorize at most one execution.

| Exit code | Meaning |
|---|---|
| 0 | Authorized; the simulated deployment ran once |
| 2 | Configuration error |
| 3 | The request could not be created |
| 10 | Rejected |
| 11 | Expired |
| 12 | Still not authorized at timeout (pending, condition or re-approval) |
| 13 | Malformed or contradictory backend answer |
| 14 | Backend unavailable |
| 15 | The gate refused (mismatch, or this request already executed once) |

**Binding:** authorization is bound to the immutable request (ID, action, summary, policy snapshot). There is no separate cryptographic action digest. The agent compares action, summary and policy itself.

## 6. End-to-end demo (mock voice; no real phone calls)

Prerequisites: PostgreSQL running and `backend/.env` set up (see `backend/README.md`). For the demo, also set:
- `VOICE_PROVIDER=mock`;
- `VOICE_MOCK_STATE_DIR=./.mock_voice`;
- `VOICE_WEBHOOK_TOKEN=<32+ random chars>`;
- `APP_ENV=development`.

1. **Migrate:** `cd backend; alembic upgrade head`.
2. **Bootstrap an organization with keys** (save the printed keys outside the repo):
   ```powershell
   python -m app.cli create-org --name "Demo" `
     --key "agent=requests:create,requests:read,audit:read,policies:read" `
     --key "admin=policies:read,policies:write,approvers:read,approvers:write,requests:read"
   python -m app.cli create-dashboard-user --org-id <org id> --email ops@example.com --name "Ops Lead" --scopes "<all dashboard scopes, see §2>"
   ```
3. **Start the API:** `uvicorn app.main:create_app --factory --port 8000`.
4. **Start the worker** in a second terminal: `python -m app.workers.voice_worker` (calls, deadlines, escalation).
5. **Start the dashboard** in a third terminal: `cd frontend; npm ci; npm run dev`. Open http://127.0.0.1:5173 and sign in.
6. **In the dashboard:** add approvers Alice and Bob with fictional numbers (`+14155550101`, `+14155550102`), then create a policy: unanimous, both approvers, 2 required, optional deadline.
7. **Start the agent** in a fourth terminal, with `APPROVAL_LINE_API_URL` and `APPROVAL_LINE_API_KEY` (the agent key) set:
   `python -m approval_line_agents.demo --policy-id <policy id>`.
   It prints the request ID and waits.
8. **Place the calls:** open the request in the dashboard and click **Schedule calls**. The worker places them; they are labelled SIMULATED.
9. **Simulate the approvers answering**, using the call IDs from the dashboard (`GET /requests/{id}/voice-calls`):
   ```powershell
   python -m app.cli voice-simulate --call-id <alice call> --decision approve
   python -m app.cli voice-simulate --call-id <bob call> --decision conditional_approve --condition "only after the database backup is verified"
   ```
   The dashboard shows "Waiting for condition". The agent keeps waiting.
10. **Resolve the condition:** in the dashboard, mark it satisfied with a note. The request now shows "Waiting for re-approval" and is still not authorized. A second call to Bob is queued.
11. **Bob re-approves:** `python -m app.cli voice-simulate --call-id <bob's new call> --decision approve`. The request becomes **Authorized**, the agent prints `AUTHORIZED` and writes `.demo_runs/deploy-<id>.json`. It exits with 0.
12. **Inspect the result:** read the audit timeline on the request page, or use the MCP tool `get_approval_timeline`.
13. **Negative paths:** run the agent again, and either answer `--decision reject` (exit 10, no record) or leave a condition unresolved (exit 12 at `--timeout`, no record).

No manual database edits are needed. Steps 9 and 11 use the real webhook endpoint with its token; `voice-simulate` refuses to run unless `VOICE_PROVIDER=mock` and `APP_ENV` is not production.

**Real Bolna mode** (`VOICE_PROVIDER=bolna`) uses the same steps, except that 9 and 11 are real phone calls. It has **not been verified** with a live call; see `voice-bolna.md` for the prerequisites.

The same flow is automated, mock-backed, in `tests/integration/test_chunk4_e2e.py`.

## 7. Known limitations

- **Live Bolna** calls, callbacks and condition free text are unverified.
- **No request idempotency:** creation has no idempotency key, so a client that retries after an ambiguous failure can create a duplicate. The demo agent and dashboard never auto-retry, and they warn when the outcome is unknown. The smallest fix is an `Idempotency-Key` header with a unique (organization, key) column.
- **Account lockout only:** dashboard sign-in has account lockout but no per-IP rate limit, and no MFA or SSO.
- **Fixed session lifetime:** absolute, with no idle timeout and no user-facing session list.
- **Same-origin deployment required:** the dashboard must be served on the same origin as the API.
- **Bounded stats:** the evaluation-derived counts look at no more than 200 pending requests, as `evaluation_complete` reports.
- **Bounded timeline:** request detail loads at most 1000 audit events.
- **Audit log not tamper-proof** (unchanged from Chunk 3): the runtime database role can bypass the triggers.
- **Polling, not push:** the dashboard does not use WebSockets or SSE.
