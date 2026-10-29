"""Read-only preflight for the live Claude Code + Approval Line + Bolna demo.

Run it from the PowerShell window you start Claude Code from (it reads the same
APPROVAL_LINE_* variables the hook will see):

    .\\scripts\\demo-doctor.ps1            (repo root; wraps this file)

Every check prints PASS / WARN / FAIL / SKIP, then a final verdict. The doctor:
- never prints secrets: API keys, tokens and phone numbers are only checked, never shown;
- places no calls and writes nothing: database queries run in a READ ONLY transaction, HTTP
  requests are GETs, plus one token-less POST to the public webhook that is refused (401)
  before anything is stored;
- reads the Bolna agent with one GET to compare its saved webhook URL and header with this
  machine's tunnel and token (no call is placed, no credit is used).
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
DEMO_DIR = REPO_DIR / "demo-target"
TUNNEL_URL_FILE = REPO_DIR / ".demo" / "tunnel-url.txt"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import Connection, Engine  # noqa: E402

from app.core.security import hash_api_key  # noqa: E402
from approval_line_agents import guard  # noqa: E402

MIN_CLAUDE_VERSION = (2, 1, 295)  # "onFailure" hook field (see docs/claude-code-guard.md)
REQUIRED_SCOPES = frozenset({"requests:create", "requests:read"})
RECOMMENDED_SCOPES = frozenset({"policies:read"})
# Scopes the guard must not hold (least privilege): voting or placing calls itself.
UNWANTED_SCOPES = frozenset({"votes:write", "voice:calls:create", "conditions:resolve"})
WEBHOOK_PATH = "/api/v1/voice/webhooks/bolna"
STALE_JOB_SECONDS = 120

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


@dataclass
class Check:
    name: str
    status: str
    detail: str


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append(Check(name, status, detail))

    @property
    def ready(self) -> bool:
        return not any(c.status == FAIL for c in self.checks)


# --- Pure checks (unit-tested) -----------------------------------------------------------------


def parse_version(output: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def check_claude_version(output: str | None) -> Check:
    name = "Claude Code version"
    if output is None:
        return Check(name, FAIL, "`claude` was not found on PATH")
    version = parse_version(output)
    minimum = ".".join(map(str, MIN_CLAUDE_VERSION))
    if version is None:
        return Check(
            name, FAIL, f"could not read a version from `claude --version` (need {minimum}+)"
        )
    shown = ".".join(map(str, version))
    if version < MIN_CLAUDE_VERSION:
        return Check(
            name, FAIL, f"{shown} is older than {minimum}: hook onFailure is ignored, fail-open"
        )
    return Check(name, PASS, f"{shown} (>= {minimum})")


def check_hook_settings(
    settings: Mapping[str, Any], demo_dir: Path, guard_timeout: float
) -> tuple[list[Check], Path | None]:
    """Validate demo-target/.claude/settings.json; return checks and the hook's python."""
    checks: list[Check] = []
    entries = settings.get("hooks", {}).get("PreToolUse", [])
    hooks = [
        (entry, hook)
        for entry in entries
        for hook in entry.get("hooks", [])
        if hook.get("args", [])[-1:] == ["approval_line_agents.guard"]
    ]
    if len(hooks) != 1:
        return [Check("Hook registration", FAIL, "exactly one guard hook expected")], None
    entry, hook = hooks[0]
    tools = set(str(entry.get("matcher", "")).split("|"))
    missing = guard.GUARDED_TOOLS - tools
    timeout = hook.get("timeout")
    problems = []
    if missing:
        problems.append(f"matcher does not cover {sorted(missing)}")
    if hook.get("onFailure") != "block":
        problems.append('onFailure is not "block" (a crash or timeout would let commands run)')
    if not isinstance(timeout, int | float) or timeout < guard_timeout + 60:
        problems.append(f"timeout must be >= guard wait + 60 s ({guard_timeout + 60:g} s)")
    checks.append(
        Check(
            "Hook registration",
            FAIL if problems else PASS,
            "; ".join(problems)
            or (f"matcher {entry.get('matcher')}, onFailure block, timeout {timeout} s"),
        )
    )
    command = str(hook.get("command", "")).replace("${CLAUDE_PROJECT_DIR}", str(demo_dir))
    python = Path(command).resolve()
    if python.is_file():
        checks.append(Check("Hook python path", PASS, str(python)))
        return checks, python
    checks.append(Check("Hook python path", FAIL, f"{python} does not exist"))
    return checks, None


def check_scopes(scopes: Iterable[str], revoked: bool) -> Check:
    name = "Agent key scopes"
    if revoked:
        return Check(name, FAIL, "the key is revoked")
    held = set(scopes)
    missing = REQUIRED_SCOPES - held
    if missing:
        return Check(name, FAIL, f"missing {sorted(missing)}")
    notes = []
    if RECOMMENDED_SCOPES - held:
        notes.append("no policies:read (the auto-dial preflight will only warn)")
    extra = sorted(UNWANTED_SCOPES & held)
    if extra:
        notes.append(f"holds {extra}: more than the guard needs")
    return Check(
        name,
        WARN if notes else PASS,
        "; ".join(notes) or "requests:create, requests:read, policies:read",
    )


def check_policy_row(
    policy_id: UUID,
    row: Mapping[str, Any] | None,
    approvers: list[Mapping[str, Any]],
    key_org: UUID | None,
) -> Check:
    name = f"Policy {policy_id}"
    if row is None:
        return Check(name, FAIL, "does not exist")
    problems = []
    if not row["is_active"]:
        problems.append("inactive")
    if not row["auto_dial"]:
        problems.append("auto-dial is OFF (no one would be called)")
    if key_org is not None and row["organization_id"] != key_org:
        problems.append("belongs to a different organization than the agent key")
    callable_count = sum(1 for a in approvers if a["is_active"] and a["has_phone"])
    if not approvers:
        problems.append("no primary approvers")
    elif callable_count < len(approvers):
        problems.append(
            f"{len(approvers) - callable_count} of {len(approvers)} approvers inactive or "
            "without a phone number"
        )
    label = f"'{row['name']}'"
    if problems:
        return Check(name, FAIL, f"{label}: " + "; ".join(problems))
    return Check(
        name, PASS, f"{label}: active, auto-dial on, {callable_count} callable approver(s)"
    )


def check_bolna_agent(
    agent: Mapping[str, Any], expected_url: str | None, token: str | None
) -> list[Check]:
    checks = []
    saved = agent.get("webhook_url")
    if expected_url is None:
        checks.append(Check("Bolna webhook URL", WARN, "no tunnel URL here to compare with"))
    elif saved == expected_url:
        checks.append(Check("Bolna webhook URL", PASS, f"saved in Bolna: {saved}"))
    else:
        shown = saved if isinstance(saved, str) else "(none)"
        checks.append(
            Check("Bolna webhook URL", FAIL, f"Bolna has {shown}; expected {expected_url}")
        )
    headers = agent.get("webhook_headers")
    supplied = headers.get("X-Approval-Line-Webhook-Token") if isinstance(headers, dict) else None
    if not token:
        checks.append(Check("Bolna webhook token", SKIP, "VOICE_WEBHOOK_TOKEN not set"))
    elif isinstance(supplied, str) and hmac.compare_digest(supplied.encode(), token.encode()):
        checks.append(Check("Bolna webhook token", PASS, "header matches VOICE_WEBHOOK_TOKEN"))
    else:
        checks.append(
            Check("Bolna webhook token", FAIL, "Bolna's header does not match VOICE_WEBHOOK_TOKEN")
        )
    return checks


# --- Environment probes (read-only) --------------------------------------------------------------


def _run(argv: list[str], timeout: float = 30) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(  # noqa: S603 - fixed argument lists, no shell
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def claude_version_output() -> str | None:
    claude = shutil.which("claude")
    if claude is None:
        return None
    result = _run([claude, "--version"])
    return (result.stdout + result.stderr) if result else ""


def worker_processes() -> int | None:
    """Count running voice workers by command line (Windows CIM; ps elsewhere)."""
    if os.name == "nt":
        result = _run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object "
                "{ $_.CommandLine -match 'app\\.workers\\.voice_worker' }).Count",
            ]
        )
        try:
            return int(result.stdout.strip()) if result else None
        except ValueError:
            return None
    result = _run(["ps", "-eo", "args"])
    return result.stdout.count("app.workers.voice_worker") if result else None


def read_only(engine: Engine) -> Connection:
    connection = engine.connect()
    connection.execute(text("SET TRANSACTION READ ONLY"))
    return connection


def _tunnel_url(env: Mapping[str, str]) -> str | None:
    value = env.get("APPROVAL_LINE_PUBLIC_URL", "").strip()
    if not value and TUNNEL_URL_FILE.is_file():
        value = TUNNEL_URL_FILE.read_text(encoding="utf-8").strip()
    return value.rstrip("/") or None


# --- The doctor ----------------------------------------------------------------------------


def run_doctor(
    env: Mapping[str, str],
    *,
    http: httpx.Client,
    claude_output: Callable[[], str | None] = claude_version_output,
    workers: Callable[[], int | None] = worker_processes,
    check_bolna: bool = True,
    settings: Any = None,
    demo_dir: Path = DEMO_DIR,
) -> Report:
    """``settings`` defaults to backend/.env; tests pass their own (and a test database)."""
    report = Report()

    # 1. Claude Code
    report.checks.append(check_claude_version(claude_output()))

    # 2. Hook registration and its interpreter
    try:
        guard_timeout = float(env.get(guard.ENV_TIMEOUT, "") or guard.DEFAULT_TIMEOUT_SECONDS)
    except ValueError:
        guard_timeout = guard.DEFAULT_TIMEOUT_SECONDS
        report.add("Guard timeout", FAIL, f"{guard.ENV_TIMEOUT} is not a number")
    settings_path = demo_dir / ".claude" / "settings.json"
    try:
        hook_settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report.add("Hook registration", FAIL, f"{settings_path} missing or not JSON")
    else:
        checks, python = check_hook_settings(hook_settings, demo_dir, guard_timeout)
        report.checks.extend(checks)
        if python is not None:
            probe = _run([str(python), "-c", "import approval_line_agents.guard"])
            ok = probe is not None and probe.returncode == 0
            report.add(
                "Hook python imports the guard",
                PASS if ok else FAIL,
                "approval_line_agents.guard importable"
                if ok
                else "import failed (pip install -e backend)",
            )

    # 3. Rules file
    rules_value = env.get(guard.ENV_RULES, "").strip()
    policy_ids: list[UUID] = []
    if not rules_value:
        report.add("Rules file", FAIL, f"{guard.ENV_RULES} is not set in this window")
    else:
        try:
            rules = guard.load_rules(rules_value)
        except guard.GuardBlockError as exc:
            report.add("Rules file", FAIL, exc.message)
        else:
            policy_ids = sorted({r.rule.policy_id for r in rules}, key=str)
            report.add("Rules file", PASS, f"{len(rules)} rule(s), {len(policy_ids)} policy ID(s)")

    # 4. Backend settings (names only)
    if settings is None:
        try:
            from app.core.config import Settings

            settings = Settings(_env_file=BACKEND_DIR / ".env")
        except Exception as exc:  # validation errors never include secret values
            report.add(
                "Backend settings", FAIL, f"backend/.env does not load ({type(exc).__name__})"
            )
            return report
    present = {
        "BOLNA_API_KEY": bool(settings.bolna_api_key and settings.bolna_api_key.get_secret_value()),
        "BOLNA_AGENT_ID": bool(settings.bolna_agent_id),
        "VOICE_WEBHOOK_TOKEN": bool(
            settings.voice_webhook_token and settings.voice_webhook_token.get_secret_value()
        ),
    }
    missing = [name for name, ok in present.items() if not ok]
    if settings.voice_provider != "bolna":
        report.add(
            "Voice provider",
            FAIL,
            f"VOICE_PROVIDER={settings.voice_provider} (live demo needs bolna; mock is the backup)",
        )
    else:
        report.add("Voice provider", PASS, "VOICE_PROVIDER=bolna")
    report.add(
        "Bolna settings",
        FAIL if missing else PASS,
        f"missing {missing}"
        if missing
        else "BOLNA_API_KEY, BOLNA_AGENT_ID, VOICE_WEBHOOK_TOKEN present",
    )
    report.add(
        "Extraction names",
        PASS,
        f"category '{settings.bolna_extraction_category}', decision "
        f"'{settings.bolna_decision_extraction}', identity '{settings.bolna_identity_extraction}', "
        f"condition '{settings.bolna_condition_extraction}' (must match the Bolna agent)",
    )

    # 5. Database: migrations, worker signal, policies, agent key
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from app.db.session import build_engine

    engine = build_engine(settings)
    key_org: UUID | None = None
    try:
        with read_only(engine) as db:
            config = Config(str(BACKEND_DIR / "alembic.ini"))
            config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
            head = ScriptDirectory.from_config(config).get_current_head()
            current = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
            report.add(
                "Migrations",
                PASS if current == head else FAIL,
                f"current {current}, head {head}"
                + ("" if current == head else " (run: alembic upgrade head)"),
            )

            stale = db.execute(
                text(
                    "SELECT count(*) FROM voice_jobs WHERE status = 'pending' "
                    "AND run_after < now() - make_interval(secs => :s)"
                ),
                {"s": STALE_JOB_SECONDS},
            ).scalar()
            count = workers()
            if count is None:
                report.add("Voice worker", WARN, "could not list processes")
            elif count == 0:
                report.add("Voice worker", FAIL, "no app.workers.voice_worker process is running")
            elif stale:
                report.add(
                    "Voice worker",
                    FAIL,
                    f"{count} running, but {stale} job(s) overdue by > {STALE_JOB_SECONDS} s",
                )
            else:
                report.add("Voice worker", PASS, f"{count} process(es), no overdue jobs")

            api_key = env.get("APPROVAL_LINE_API_KEY", "").strip()
            if not api_key:
                report.add("Agent key", FAIL, "APPROVAL_LINE_API_KEY is not set in this window")
            else:
                row = (
                    db.execute(
                        text(
                            "SELECT organization_id, scopes, revoked_at IS NOT NULL AS revoked "
                            "FROM api_keys WHERE key_hash = :h"
                        ),
                        {"h": hash_api_key(api_key)},
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    report.add("Agent key", FAIL, "not found in this database")
                else:
                    key_org = row["organization_id"]
                    report.checks.append(check_scopes(row["scopes"], row["revoked"]))

            for policy_id in policy_ids:
                policy = (
                    db.execute(
                        text(
                            "SELECT name, is_active, auto_dial, organization_id "
                            "FROM approval_policies WHERE id = :p"
                        ),
                        {"p": policy_id},
                    )
                    .mappings()
                    .first()
                )
                approvers = list(
                    db.execute(
                        text(
                            "SELECT a.is_active, a.phone_number IS NOT NULL AS has_phone "
                            "FROM policy_approvers pa JOIN approvers a ON a.id = pa.approver_id "
                            "WHERE pa.policy_id = :p AND pa.role = 'primary'"
                        ),
                        {"p": policy_id},
                    ).mappings()
                )
                report.checks.append(check_policy_row(policy_id, policy, approvers, key_org))
    except Exception as exc:
        report.add("Database", FAIL, f"read-only checks failed ({type(exc).__name__})")
    finally:
        engine.dispose()

    # 6. API, agent key over HTTP, public tunnel
    api_url = env.get("APPROVAL_LINE_API_URL", "").strip().rstrip("/") or "http://127.0.0.1:8000"

    def get_ok(url: str, **kwargs: Any) -> tuple[bool, str]:
        try:
            response = http.get(url, timeout=10, **kwargs)
        except httpx.HTTPError as exc:
            return False, type(exc).__name__
        return response.status_code == 200, f"HTTP {response.status_code}"

    ok, detail = get_ok(f"{api_url}/api/v1/health/ready")
    report.add("API ready", PASS if ok else FAIL, f"{api_url}: {detail}")
    if api_key := env.get("APPROVAL_LINE_API_KEY", "").strip():
        ok, detail = get_ok(f"{api_url}/api/v1/requests?limit=1", headers={"X-API-Key": api_key})
        report.add("Agent key over HTTP", PASS if ok else FAIL, f"GET /requests: {detail}")

    tunnel = _tunnel_url(env)
    expected_webhook = f"{tunnel}{WEBHOOK_PATH}" if tunnel else None
    if tunnel is None:
        report.add(
            "Public tunnel",
            FAIL,
            "no URL (set APPROVAL_LINE_PUBLIC_URL or start the tunnel with start-demo.ps1)",
        )
    else:
        ok, detail = get_ok(f"{tunnel}/api/v1/health/ready")
        report.add("Public tunnel", PASS if ok else FAIL, f"{tunnel}: {detail}")
        try:
            # No token: refused with 401 before anything is stored. 404 = voice disabled.
            response = http.post(
                expected_webhook, content=b"{}", headers={"Content-Type": "application/json"}
            )
            status = response.status_code
        except httpx.HTTPError as exc:
            status, detail = None, type(exc).__name__
        report.add(
            "Public webhook route",
            PASS if status == 401 else FAIL,
            "reachable, token required (401)"
            if status == 401
            else f"unexpected: {status or detail}",
        )

    # 7. Bolna agent (one read-only GET; no call)
    if not check_bolna or settings.voice_provider != "bolna" or missing:
        report.add(
            "Bolna agent",
            SKIP,
            "not checked: confirm the webhook URL saved in Bolna is "
            f"{expected_webhook or '<tunnel>' + WEBHOOK_PATH}",
        )
    else:
        try:
            response = http.get(
                f"{settings.bolna_api_base_url.rstrip('/')}/v2/agent/{settings.bolna_agent_id}",
                headers={"Authorization": f"Bearer {settings.bolna_api_key.get_secret_value()}"},
                timeout=15,
            )
            agent = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            agent = None
        if not isinstance(agent, dict):
            report.add(
                "Bolna agent",
                WARN,
                "could not read the agent; confirm by hand that Bolna's webhook URL is "
                f"{expected_webhook or '<tunnel>' + WEBHOOK_PATH}",
            )
        else:
            token = settings.voice_webhook_token
            report.checks.extend(
                check_bolna_agent(
                    agent, expected_webhook, token.get_secret_value() if token else None
                )
            )
    return report


def render(report: Report) -> str:
    width = max(len(c.name) for c in report.checks)
    lines = [f"[{c.status}] {c.name.ljust(width)}  {c.detail}" for c in report.checks]
    verdict = "READY for the live demo" if report.ready else "NOT READY: fix every FAIL above"
    warns = sum(c.status == WARN for c in report.checks)
    lines += ["", f"Verdict: {verdict}" + (f" ({warns} warning(s))" if warns else "")]
    lines.append(
        "Reminder: Bolna webhook headers and phone-number verification live in the Bolna "
        "dashboard; re-check them after any tunnel restart."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--no-bolna", action="store_true", help="Skip the Bolna agent GET.")
    args = parser.parse_args(argv)
    with httpx.Client(follow_redirects=False) as http:
        report = run_doctor(os.environ, http=http, check_bolna=not args.no_bolna)
    print(render(report))
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
