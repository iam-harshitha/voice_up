# Local development and demo runbook

This runbook shows the guarantee Approval Line exists to provide: **an AI agent runs its proposed action only after the backend reports that exact request as `approved` with `authorized: true`.** The only action in this demo is a harmless local simulation, which writes a JSON record.

**Mock mode.** With `VOICE_PROVIDER=mock`, every phone call is **simulated**:
- No real call is placed.
- You play the approvers with `python -m app.cli voice-simulate`.
- Each answer still goes through the real webhook endpoint (token-checked), the durable job queue and the policy engine.
- The dashboard labels these calls "SIMULATED VOICE".

## 1. Prerequisites (once)

| Need | Check |
|---|---|
| Python 3.12 virtualenv at `env\`, with `pip install -e ".[dev]"` run in `backend\` | `env\Scripts\python.exe --version` |
| PostgreSQL 14+ with the dev and test databases (see `backend/README.md`) | `psql` connects |
| Node.js 20+; `npm ci` run in `frontend\` | `node --version` |
| `backend\.env` copied from `.env.example` | contains `DATABASE_URL` and `TEST_DATABASE_URL` |

**Mock-voice settings in `backend\.env`:**
- `VOICE_PROVIDER=mock`
- `VOICE_MOCK_STATE_DIR=./.mock_voice`
- `VOICE_WEBHOOK_TOKEN=<32+ random characters>`, generated with `python -c "import secrets;print(secrets.token_urlsafe(32))"`

## 2. Start everything (one command)

From the repository root, in PowerShell:

```powershell
.\scripts\start-demo.ps1 -Migrate
```

**What the script does:**
- Checks the prerequisites.
- Reports each setting as **PRESENT/ABSENT** without showing values, and prints whether voice is SIMULATED (mock) or REAL (Bolna).
- **Refuses to start** if port 8000 or 5173 is busy, if a worker is already running (`-SkipWorker` reuses it), or if a previous demo is still recorded.
- Runs `alembic upgrade head`. This is additive and never drops anything.
- Starts the API, worker and dashboard in the background, logging to `.demo\logs\`.
- Waits for `GET /api/v1/health/ready`.

Use `-ApiPort` and `-WebPort` to pick other ports. Stop with `.\scripts\stop-demo.ps1`. It stops only the processes the start script recorded and leaves the database and logs alone.

**Starting each process by hand** (equivalent):

```powershell
cd backend; alembic upgrade head
uvicorn app.main:create_app --factory --port 8000          # terminal 1
python -m app.workers.voice_worker                         # terminal 2 (the API never runs jobs)
cd ..\frontend; npm run dev                                # terminal 3 -> http://127.0.0.1:5173
```

**Readiness check:** `Invoke-RestMethod http://127.0.0.1:8000/api/v1/health/ready` should return `{status: ok, database: ok}`.

## 3. Seed the demo (once per organization)

`demo-seed` works only on an **existing** organization that you name explicitly. It is additive and idempotent:
- It reuses identical records and refuses to change conflicting ones.
- It never creates requests or votes.
- It is refused when `APP_ENV=production`.

```powershell
cd backend
python -m app.cli create-org --name "Demo"                          # prints the org id
python -m app.cli demo-seed --org-id <org id> --yes --issue-agent-key
python -m app.cli create-dashboard-user --org-id <org id> --email you@example.com --name "You" `
  --scopes "approvers:read,approvers:write,audit:read,conditions:resolve,policies:read,policies:write,requests:create,requests:read,voice:calls:create,voice:calls:read"
```

**What gets created:**
- **Approvers:** Demo Alice, Demo Bob and Demo Carol (backup), with fictional `+1415555010x` numbers.
- **"Demo: production deploys":** unanimous, Alice and Bob, 1-hour deadline.
- **"Demo: short deadline":** the same approvers, 2-minute deadline.
- **Agent key:** a `demo-agent` key (`requests:create, requests:read, audit:read, policies:read`), printed **once**.

The dashboard user can never hold `votes:write`.

Set the agent's environment in the terminal where you run the agent:

```powershell
$env:APPROVAL_LINE_API_URL = "http://127.0.0.1:8000"
$env:APPROVAL_LINE_API_KEY = "<the demo-agent key>"
```

Sign in at http://127.0.0.1:5173.

## 4. Scenarios

In each scenario:
- The agent runs in one terminal.
- You act as the operator in the dashboard and as the approvers with `voice-simulate`.
- Call IDs appear on the request page in the dashboard (Voice calls), or come from `GET /requests/{id}/voice-calls`.

```powershell
python -m approval_line_agents.demo --policy-id <production policy id>       # MCP (default)
python -m approval_line_agents.demo --policy-id <id> --transport rest         # REST
```

The agent prints the proposed action, its idempotency key and the **request ID**. It then waits, with bounded polling, until the request is final or its timeout passes.

### A. Approved after a condition and re-approval (the flagship)
1. Open the request in the dashboard and click **Schedule calls**. Two SIMULATED calls are queued and placed by the worker.
2. Alice approves: `python -m app.cli voice-simulate --call-id <Alice's call> --decision approve`
   - Dashboard: 1/2, not authorized. The agent keeps waiting.
3. Bob approves on a condition: `python -m app.cli voice-simulate --call-id <Bob's call> --decision conditional_approve --condition "only after the database backup is verified"`
   - Dashboard: **Waiting for condition**, not authorized.
4. In the dashboard, resolve the condition as satisfied, with a note.
   - Dashboard: **Waiting for re-approval**, still not authorized. A second call to Bob is queued.
5. Bob re-approves: `python -m app.cli voice-simulate --call-id <Bob's new call> --decision approve`

**Expected agent output (abridged, from a verified run):**

```
Approval request created: 5c4a132b-...
  status=pending authorized=false state=awaiting_approvals ... approvals=0/2
  status=pending authorized=false ... approvals=1/2
  status=pending authorized=false state=waiting_for_condition reason=condition_open ...
  status=pending authorized=false state=waiting_for_condition reason=reapproval_required ...
  status=approved authorized=true state=approved reason=quorum_met next_action=none approvals=2/2
AUTHORIZED. Simulated deployment executed; record: .demo_runs\deploy-5c4a132b-....json
```

The exit code is **0**. The record contains `"simulated": true` and `authorized_by_request_id`. Running the agent again with `--resume <request id>` exits **15**: one request authorizes at most one execution **per record directory** (`--record-dir`, default `.demo_runs` under the working directory). Running it from another directory, with another `--record-dir`, or on another machine is not protected (see `security.md`).

### B. Rejected
Schedule the calls. Alice approves (`--decision approve`) and Bob rejects (`--decision reject`). The agent prints `HALTED: rejected; not deploying` and exits **10**. No record is written.

### C. Condition never resolved
Run the agent with `--timeout 25`. Alice approves and Bob gives a conditional approval; nobody resolves it. The agent prints `HALTED: timed out while still not authorized` and exits **12**.

### D. Voice failure (simulated)
Use `voice-simulate --call-id <id> --status failed` for one call, and `--decision none` for the other (the call ends without an explicit decision).
- **Dashboard:** "Failed" and "No explicit decision". Neither call is a vote.
- **Agent:** exits **12** at its timeout.

### E. Deadline passed
Run the agent against the **short deadline** policy, with `--timeout 220`, and do nothing. After 2 minutes the evaluation reports `deadline_passed`. The agent exits **11** and the worker marks the request `expired`.

### F. Ambiguous creation, recovered safely
If creation times out, the agent prints its key. Re-running with `--idempotency-key <key>`, over either transport, returns **the same request ID** instead of creating a duplicate. This was verified live: two runs with one key produced one request.

### Audit timeline
The request page shows the persisted timeline in `sequence` order: creation, calls, votes, condition lifecycle, decision. The MCP tool `get_approval_timeline` returns the same events. In the verified run, both showed 19 events, with no phone numbers or keys.

## 5. Fallbacks for a live presentation

| Problem | Fallback |
|---|---|
| No network or no Bolna | Use mock mode, labelled SIMULATED. It needs only localhost. |
| Real call does not connect | Show the call's `failed` / `no_decision` state; the agent stays blocked. |
| Time pressure | Run `pytest tests/integration/test_chunk4_e2e.py tests/integration/test_chunk5_reliability.py -q` (mock-backed, about 1 minute). |
| Dashboard trouble | Use `GET /api/v1/requests/{id}` and the MCP tools; both read the same request. |

## 6. Stop

```powershell
.\scripts\stop-demo.ps1
```

To retire the demo credentials, revoke the key with `python -m app.cli revoke-key --key-id <id>` and disable the user with `python -m app.cli disable-dashboard-user --email <email>`.
