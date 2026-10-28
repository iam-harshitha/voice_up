# Measured performance (local, bounded)

These numbers come from `backend/scripts/measure_latency.py`, run on 2026-10-26.

**Conditions:**
- **Machine:** the development laptop (Windows 11, 12 logical CPUs), busy with other applications (about 37% CPU load).
- **API:** one uvicorn process, separate from the measuring client, serving over HTTP on 127.0.0.1.
- **Database:** local PostgreSQL 18, isolated test database, with fewer than 200 requests in the measured organization.
- **Voice:** the mock provider.

**This is not a load test, and it says nothing about production capacity.**

| What | Samples | p50 | p95 | max |
|---|---|---|---|---|
| `POST /requests` with `Idempotency-Key` | 100 | 39.9 ms | 52.8 ms | 171 ms |
| Idempotent replay of the same key | 100 | 25.6 ms | 30.5 ms | 42 ms |
| `GET /requests/{id}` (detail and evaluation) | 100 | 22.8 ms | 24.8 ms | 26 ms |
| `GET /requests?include_evaluation=true&limit=50` | 20 | 171 ms | 234 ms | 246 ms |
| `GET /requests/stats` (101 pending requests evaluated) | 20 | 308 ms | 386 ms | 416 ms |
| `approval_service.evaluate` in-process | 100 | 3.4 ms | 4.9 ms | 12 ms |
| MCP `get_approval_status` round trip (in-memory MCP session, then HTTP) | 100 | 31.6 ms | 156 ms | 522 ms |
| Worker: drain 40 `initiate_call` jobs (mock provider) | 40 jobs | | | 1642 ms total (41 ms per job) |
| `GET` detail, 8 concurrent clients | 400 | 447 ms | 863 ms | 1273 ms (13 req/s, 0 errors) |

**Reading these numbers:**
- **Concurrent reads are limited by the environment, not the application.** On this machine, HTTP throughput was capped at about 50–400 requests per second even for a bare FastAPI "hello" app, and the cap varied from run to run. In one back-to-back run, both the bare app and `/health/live` measured about 55 req/s.
- **Application overhead is small.** In-process, through the ASGI interface with no network, Approval Line's middleware stack costs about 1.07 ms per request, against 0.92 ms for the bare app.
- **No optimization was made,** because none of this demonstrates an application bottleneck.
- **Stats and list-with-evaluation grow with the work they do.** They evaluate each pending request (stats caps this at 200 requests and reports `evaluation_complete`). That is acceptable for a dashboard; it is the first place to optimize if pending volume grows.

**Re-run:** `cd backend; python scripts/measure_latency.py --samples 100 --concurrency 8`. It refuses any database whose name doesn't contain "test". Don't run it while pytest is running, because both share the test database.
