# Production deployment guide

**Status:** the artifacts below are prepared and their configuration logic is tested, but **nothing has been deployed**. The Docker images and nginx config have **not been built or run here** (Docker isn't installed on the development machine). Building them, provisioning a host and database, configuring DNS and TLS, and live Bolna verification all still need your account and your explicit go-ahead.

## Architecture (smallest shape that fits)

```
 internet ──HTTPS──> TLS terminator / load balancer
                        └─> web (nginx: dashboard static files, /api/ → api:8000)   one origin
 api (uvicorn, FastAPI)  ──┐
 worker (voice + deadline/escalation jobs, separate process) ──┼──> PostgreSQL 14+ (managed, backed up)
 migrate (one-shot: alembic upgrade head) ──┘
 Bolna ──HTTPS webhook──> /api/v1/voice/webhooks/bolna (token header; IP allowlist at the edge)
```

**Images:**

| Service | Image | Command |
|---|---|---|
| `api` | `backend/Dockerfile` | the default `uvicorn` command |
| `worker` | the **same** image | `python -m app.workers.voice_worker` (the API never runs jobs) |
| `web` | `frontend/Dockerfile` | builds the bundle (dependencies installed inside the Linux image; `frontend/.dockerignore` keeps host `node_modules`, `dist` and env files out), runs the secret scan, then serves it with nginx (`frontend/nginx.conf`). Every location includes `nginx-security-headers.conf`: CSP, `X-Frame-Options`, nosniff, no-referrer, HSTS |
| `migrate` | the backend image | `alembic upgrade head` |

`deploy/docker-compose.prod.yml` shows the shape; adapt it to your platform. The dashboard **must be on the same origin** as `/api`: the session cookie is `SameSite=Strict` and scoped to `Path=/api/v1`.

## Configuration

Inject everything through your platform's secret manager. Keep `deploy/.env.production` git-ignored (all `.env.*` files are ignored except `.env.example`).

| Variable | Production value |
|---|---|
| `APP_ENV` | `production`. Disables `/docs` and `/openapi.json`; refuses unsafe settings (below). `deploy/docker-compose.prod.yml` pins it on `migrate`, `api` and `worker` (`environment` overrides `env_file`), so the env file can't switch it off. On other platforms, set it in the service definition too, not only in a secrets file. |
| `DATABASE_URL` | `postgresql+psycopg://...` for the production database. **Secret.** |
| `LOG_LEVEL` | `INFO` |
| `CORS_ORIGINS` | Usually empty: the dashboard is same-origin. If set, use only explicit `https://` origins. |
| `DASHBOARD_COOKIE_SECURE` | Leave empty (defaults to on in production; `false` is refused). |
| `DASHBOARD_SESSION_TTL_SECONDS`, `DASHBOARD_MAX_FAILED_LOGINS`, `DASHBOARD_LOCKOUT_SECONDS` | Defaults: 8 h, 5 failures, 15 min. |
| `VOICE_PROVIDER` | `bolna` or `disabled`. `mock` is refused in production. |
| `BOLNA_API_KEY`, `BOLNA_AGENT_ID`, `VOICE_WEBHOOK_TOKEN` (32+ characters) | Required with `bolna`. **Secrets.** |
| `BOLNA_API_BASE_URL` | Must be `https://`. |

**Settings the API refuses to start with** in production (each is tested in `tests/unit/test_dashboard_security.py`):
- `VOICE_PROVIDER=mock`;
- `DASHBOARD_COOKIE_SECURE=false`;
- wildcard or `http://` `CORS_ORIGINS`;
- `bolna` without its key, agent ID or a 32+ character webhook token;
- an `http://` Bolna URL.

Error responses never include stack traces or exception text. Unhandled errors become a generic 500 with a request ID.

**Credentials:**
- Create agent keys with `python -m app.cli create-key`, with least privilege: `requests:create,requests:read`.
- Create dashboard users with `create-dashboard-user`; they can never hold `votes:write`.
- No default credentials exist.

## Validating the images and nginx before a release

None of this has been run here (no Docker on the development machine). Run it in an environment that has Docker:

```bash
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.production config   # resolved config; check APP_ENV=production on migrate/api/worker
docker build -t approval-line-web frontend                                                    # fails if the bundle secret scan fails
docker run --rm --add-host api:127.0.0.1 approval-line-web nginx -t                           # nginx syntax + includes resolve
docker run --rm -d -p 8080:8080 --add-host api:127.0.0.1 --name alw approval-line-web
curl -sI http://127.0.0.1:8080/ | grep -iE "content-security-policy|x-frame-options|x-content-type-options|referrer-policy|strict-transport|cache-control"
curl -sI http://127.0.0.1:8080/assets/<a built file> | grep -iE "content-security-policy|cache-control"   # one Cache-Control: public, max-age=31536000, immutable
docker rm -f alw
```

**Expected:** every response, including `/` and `/assets/…`, carries all five security headers and exactly one `Cache-Control`.

Without Docker, `npm test` in `frontend/` runs static checks of these files (`src/test/deploy-config.test.ts`), and `pytest tests/unit/test_deploy_config.py` checks the `APP_ENV` pin. Those catch the reviewed mistakes, but they don't replace `nginx -t` or a real build.

## Release procedure

1. **Back up the database** (see Backup) and record the current revision with `alembic current`.
2. **Build** the images from the release commit. The web build fails if the bundle scan finds a secret.
3. **Migrate:** run `migrate` (`alembic upgrade head`) **before** starting new `api` and `worker` versions. Migrations 0004 (dashboard tables) and 0005 (two nullable columns plus a unique index) are additive, so old API versions keep working against the new schema during a rolling update.
   - 0005 builds its index without `CONCURRENTLY`. On a very large `approval_requests` table, schedule that step.
   - **Never** run the test suite against the production database. The test fixtures drop the schema of `TEST_DATABASE_URL` and refuse database names without "test".
4. **Roll out** `api`, then `worker`. Running more than one worker is safe: jobs are leased with `FOR UPDATE SKIP LOCKED`.
5. **Validate:**
   - `GET /api/v1/health/live` returns 200 (the process is up).
   - `GET /api/v1/health/ready` returns 200 with `{"database": "ok"}`, or 503 without credentials in the body.
   - The dashboard loads and sign-in works.
   - A test request created with an agent key appears in the dashboard.
   - The worker logs show job claims.

## Rollback

- **Application:** redeploy the previous images. The schema remains compatible because 0004 and 0005 are additive.
- **Schema:**
  - `alembic downgrade 0004` drops the idempotency columns. It loses only replay protection for keys already used.
  - `downgrade 0003` drops the dashboard tables, which ends sign-ins and sessions.
  - Downgrading below 0003 is **refused** once Chunk 3 data exists (by design, so authorization history is never discarded).
  - Prefer restoring a backup to downgrading in production.

## Backup and restore

- Use your managed database's point-in-time recovery, or run scheduled `pg_dump --format=custom` with an off-site copy.
- Test a restore regularly: `pg_restore --clean --if-exists -d <scratch db>`, then `alembic current`, then a smoke test against the restored database.
- Audit events are append-only at the application layer, and a trigger blocks UPDATE and DELETE.
- Backups are the recovery path for operator error. They are **not** tamper evidence (see `security.md`).

## Logs and monitoring

- **Format:** JSON lines on stdout, one `request.completed` line per request (method, path, status, duration, request ID).
- **Never logged:** headers, bodies, API keys, cookies, passwords and phone numbers.
- **Worker events:** `voice.job_claimed`, `voice.call_initiated`, retries and `workflow.*`.
- **Alert on:**
  - a failing readiness check;
  - a growing number of `voice_jobs` in status `failed`;
  - calls stuck in `unknown`.

## Still requires your action

1. Choose a host and managed PostgreSQL, then build and push the images. **Not done; no resources were created.**
2. Set up DNS and a TLS certificate on the terminator.
3. Store the secrets in the platform's secret manager.
4. Configure Bolna: an agent with the extractions, and the webhook URL plus token header (`voice-bolna.md`). Ideally restrict the webhook to Bolna's IPs at the edge.
5. Run the live Bolna checklist with a consented test phone before relying on real calls.
6. Recommended hardening: run migrations as a separate owner role, and give the runtime role only `SELECT, INSERT` on `audit_events`, `request_approvers` and `escalation_steps` (see `policies.md`).
