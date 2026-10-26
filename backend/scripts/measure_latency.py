"""Bounded local latency measurements for Approval Line (Chunk 5).

Runs the REAL application (FastAPI + PostgreSQL + policy engine + mock voice worker)
against the isolated TEST database only, over real HTTP on 127.0.0.1, and prints
sample counts with p50 / p95 / max. It is not a load test and says nothing about
production capacity.

Safety: refuses unless TEST_DATABASE_URL's database name contains "test" and differs
from DATABASE_URL. Creates its own organization; never deletes anything (the next
pytest run resets the test schema). Do not run it while pytest is running: both use
the same test database.

    cd backend
    python scripts/measure_latency.py [--samples 100] [--concurrency 8]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings
from app.core.security import AGENT_SCOPES
from app.db.session import build_engine, build_session_factory, transaction
from app.services import api_key_service, approval_service
from app.services.voice_job_service import run_pending_jobs
from app.voice.mock import MockVoiceProvider

PORT = 8765


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def report(name: str, samples_ms: list[float], note: str = "") -> None:
    print(
        f"  {name:<46} n={len(samples_ms):<4} p50={statistics.median(samples_ms):7.2f} ms  "
        f"p95={pct(samples_ms, 0.95):7.2f} ms  max={max(samples_ms):7.2f} ms {note}"
    )


def timed(fn) -> float:
    start = time.perf_counter()
    fn()
    return (time.perf_counter() - start) * 1000


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()

    base = Settings()  # reads backend/.env; values are never printed
    if not base.test_database_url:
        print("TEST_DATABASE_URL is not set; nothing measured.")
        return 2
    test_db = make_url(base.test_database_url).database or ""
    if "test" not in test_db or test_db == make_url(base.database_url).database:
        print("Refusing: TEST_DATABASE_URL must name a separate database containing 'test'.")
        return 2
    settings = Settings(
        _env_file=None,
        database_url=base.test_database_url,
        app_env="test",
        log_level="WARNING",
        voice_provider="mock",
        voice_webhook_token="m" * 40,
    )

    engine = build_engine(settings)
    sessions = build_session_factory(engine)
    with sessions() as db, transaction(db):
        org = api_key_service.create_organization(db, f"latency-{uuid.uuid4().hex[:8]}")
        admin = api_key_service.issue_api_key(
            db,
            org.id,
            "admin",
            "policies:read,policies:write,approvers:read,approvers:write,requests:read,"
            "voice:calls:create,voice:calls:read",
        )
        agent = api_key_service.issue_api_key(db, org.id, "agent", sorted(AGENT_SCOPES))
        org_id = org.id

    # The API runs in its OWN process, so client threads here do not share its GIL.
    server_env = {
        **os.environ,
        "DATABASE_URL": base.test_database_url,
        "APP_ENV": "test",
        "LOG_LEVEL": "WARNING",
        "VOICE_PROVIDER": "mock",
        "VOICE_WEBHOOK_TOKEN": "m" * 40,
        "CORS_ORIGINS": "",
    }
    server = subprocess.Popen(  # noqa: S603 - fixed interpreter and arguments
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
            "--log-level",
            "warning",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=server_env,
    )
    api = f"http://127.0.0.1:{PORT}/api/v1"
    for _ in range(200):
        try:
            if httpx.get(f"{api}/health/ready", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    try:
        return measure(args, settings, sessions, org_id, admin, agent, api)
    finally:
        server.terminate()
        server.wait(10)
        engine.dispose()


def measure(args, settings, sessions, org_id, admin, agent, api) -> int:

    port = int(api.split(":")[2].split("/")[0])
    admin_h = {"X-API-Key": admin.plaintext}
    agent_h = {"X-API-Key": agent.plaintext}
    with httpx.Client(timeout=10) as http:
        approvers = [
            http.post(
                f"{api}/approvers",
                json={"name": n, "phone_number": p},
                headers=admin_h,
            ).json()["id"]
            for n, p in (("Alice", "+14155550101"), ("Bob", "+14155550102"))
        ]
        policy = http.post(
            f"{api}/policies",
            json={"name": "Measure", "required_approvals": 2, "approver_ids": approvers},
            headers=admin_h,
        ).json()["id"]

        print(
            "Environment: local Windows dev machine; API (one uvicorn process, separate from "
            f"this client) and PostgreSQL on localhost; test database. samples={args.samples}"
        )
        print("Results (wall-clock latency measured by the client, including HTTP):")

        ids: list[str] = []

        def create():
            r = http.post(
                f"{api}/requests",
                json={"policy_id": policy, "action": "deploy_to_production", "summary": "m"},
                headers={**agent_h, "Idempotency-Key": f"measure-{uuid.uuid4().hex}"},
            )
            r.raise_for_status()
            ids.append(r.json()["id"])

        creates = [timed(create) for _ in range(args.samples)]
        report("POST /requests (with Idempotency-Key)", creates)
        replay_key = f"measure-replay-{uuid.uuid4().hex}"
        payload = {"policy_id": policy, "action": "deploy_to_production", "summary": "m"}
        replay_headers = {**agent_h, "Idempotency-Key": replay_key}
        http.post(f"{api}/requests", json=payload, headers=replay_headers)
        report(
            "POST /requests idempotent replay",
            [
                timed(
                    lambda: http.post(
                        f"{api}/requests",
                        json=payload,
                        headers={**agent_h, "Idempotency-Key": replay_key},
                    ).raise_for_status()
                )
                for _ in range(args.samples)
            ],
        )
        target = ids[0]
        report(
            "GET /requests/{id} (detail + evaluation)",
            [
                timed(
                    lambda: http.get(f"{api}/requests/{target}", headers=agent_h).raise_for_status()
                )
                for _ in range(args.samples)
            ],
        )
        report(
            "GET /requests?include_evaluation=true&limit=50",
            [
                timed(
                    lambda: http.get(
                        f"{api}/requests",
                        params={"include_evaluation": "true", "limit": 50},
                        headers=agent_h,
                    ).raise_for_status()
                )
                for _ in range(max(10, args.samples // 5))
            ],
        )
        pending = len(ids) + 1
        report(
            "GET /requests/stats",
            [
                timed(lambda: http.get(f"{api}/requests/stats", headers=agent_h).raise_for_status())
                for _ in range(max(10, args.samples // 5))
            ],
            f"({pending} pending requests evaluated per call)",
        )

        # Concurrent reads.
        per_worker = max(5, args.samples // 2)

        def reader(_):
            out = []
            with httpx.Client(timeout=10) as c:
                for _ in range(per_worker):
                    start = time.perf_counter()
                    r = c.get(f"{api}/requests/{target}", headers=agent_h)
                    out.append(((time.perf_counter() - start) * 1000, r.status_code))
            return out

        start = time.perf_counter()
        with ThreadPoolExecutor(args.concurrency) as pool:
            results = [x for chunk in pool.map(reader, range(args.concurrency)) for x in chunk]
        elapsed = time.perf_counter() - start
        errors = sum(1 for _, code in results if code != 200)
        report(
            f"GET detail, {args.concurrency} concurrent clients",
            [ms for ms, _ in results],
            f"({len(results) / elapsed:.0f} req/s, {errors} errors)",
        )

    # Engine evaluation in-process (no HTTP).
    with sessions() as db:
        request = approval_service.get_request(db, org_id, uuid.UUID(target))
        evals = [timed(lambda: approval_service.evaluate(db, request)) for _ in range(args.samples)]
        db.rollback()
    report("approval_service.evaluate (in-process, DB reads)", evals)

    # MCP tool round trip (in-memory MCP session -> real HTTP -> API).
    from mcp import Client

    from approval_line_agents.client import ApprovalLineClient, ClientSettings
    from approval_line_agents.mcp_server import build_server

    async def mcp_round_trips() -> list[float]:
        out = []
        async with (
            ApprovalLineClient(
                ClientSettings(f"http://127.0.0.1:{port}", agent.plaintext, 10)
            ) as client,
            Client(build_server(client)) as session,
        ):
            for _ in range(args.samples):
                start = time.perf_counter()
                result = await session.call_tool("get_approval_status", {"request_id": target})
                if result.is_error:
                    raise RuntimeError("MCP tool call failed")
                out.append((time.perf_counter() - start) * 1000)
        return out

    report("MCP get_approval_status round trip", asyncio.run(mcp_round_trips()))

    # Worker: schedule mock calls, then drain the queue once and time it.
    calls_for = ids[: min(20, len(ids))]
    with httpx.Client(timeout=10) as http:
        for rid in calls_for:
            url = f"{api}/requests/{rid}/voice-calls"
            http.post(url, json={}, headers=admin_h).raise_for_status()
    provider = MockVoiceProvider(settings)
    start = time.perf_counter()
    jobs = run_pending_jobs(sessions, provider, settings, "measure", max_jobs=1000)
    drain = (time.perf_counter() - start) * 1000
    ok = sum(1 for _, result in jobs if result == "initiated")
    print(
        f"  {'worker: drain initiate_call jobs (mock provider)':<46} jobs={len(jobs)} "
        f"initiated={ok} total={drain:.0f} ms ({drain / max(1, len(jobs)):.1f} ms/job)"
    )

    print(
        "Limitations: one machine, localhost network, tiny data set, mock provider; "
        "not representative of production capacity."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
