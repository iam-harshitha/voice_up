"""``app.cli connect-claude``: connect a Claude Code project to an Approval Line policy.

Writes the two git-ignored files the guard needs, so no window needs APPROVAL_LINE_*
variables set by hand:

- ``<project>/.claude/settings.local.json``: an "env" block with APPROVAL_LINE_API_URL,
  APPROVAL_LINE_API_KEY and APPROVAL_LINE_GUARD_RULES. Claude Code passes it to the hook
  and the MCP server (see approval_line_agents/local_config.py). Other settings are kept.
- ``<project>/guard-rules.json``: the project's guard-rules.example.json with every
  policy-ID placeholder replaced by the given policy.

Every check runs before anything is written or issued: the project has the guard hook
and the example rules; both target files are git-ignored (the settings file holds the
key); the API is healthy; the policy exists, is active and has auto-dial on. The agent
key gets only the scopes the guard needs. It is never printed. A previous guard key found
in the settings file is revoked once the new one is written.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_api_key
from app.db.models import ApiKey, ApprovalPolicy
from app.db.session import transaction
from app.services import api_key_service
from approval_line_agents import local_config
from approval_line_agents.guard import RulesFile

KEY_NAME = "claude-code-guard"
GUARD_SCOPES = ("requests:create", "requests:read", "policies:read")
EXAMPLE_RULES = "guard-rules.example.json"
RULES_FILE = "guard-rules.json"
GUARD_MODULE = "approval_line_agents.guard"
_PLACEHOLDER = re.compile(r"<[A-Z_]+_POLICY_ID>")


class ConnectError(Exception):
    """Nothing was written. ``message`` is safe to print (no secrets)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ConnectResult:
    project_dir: Path
    settings_file: Path
    rules_file: Path
    rule_count: int
    policy_id: UUID
    policy_name: str
    organization_id: UUID
    key_id: UUID
    revoked_key_id: UUID | None


def _check_hook(project_dir: Path) -> None:
    path = project_dir / ".claude" / "settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8-sig"))
        hooks = [
            hook
            for entry in settings["hooks"]["PreToolUse"]
            for hook in entry.get("hooks", [])
            if hook.get("args", [])[-1:] == [GUARD_MODULE]
        ]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        hooks = []
    if not hooks:
        raise ConnectError(
            f"{path} does not register the Approval Line guard as a PreToolUse hook. "
            "Use a project set up like demo-target."
        )


def _rules_text(project_dir: Path, policy_id: UUID) -> tuple[str, int]:
    example = project_dir / EXAMPLE_RULES
    try:
        text = example.read_text(encoding="utf-8-sig")
    except OSError:
        raise ConnectError(f"{example} is missing.") from None
    text = _PLACEHOLDER.sub(str(policy_id), text)
    try:
        parsed = RulesFile.model_validate(json.loads(text))
    except (ValueError, ValidationError):
        raise ConnectError(f"{example} is not a valid rules file.") from None
    return text, len(parsed.rules)


def _require_ignored(path: Path) -> None:
    ignored = local_config.is_git_ignored(path)
    if ignored is None:
        raise ConnectError(
            f"cannot confirm that {path} is git-ignored (git unavailable or not a "
            "repository). Refusing to write the agent key there."
        )
    if not ignored:
        raise ConnectError(
            f"{path} is not git-ignored. Refusing to write the agent key there: add it to "
            ".gitignore first."
        )


def _check_api(http: httpx.Client, api_url: str) -> None:
    url = f"{api_url}/api/v1/health/ready"
    try:
        response = http.get(url, timeout=10)
    except httpx.HTTPError as exc:
        raise ConnectError(
            f"the API at {api_url} is not reachable ({type(exc).__name__}). Start it first "
            "(scripts\\start-demo.ps1)."
        ) from None
    if response.status_code != 200:
        raise ConnectError(f"the API at {api_url} is not ready (HTTP {response.status_code}).")


def _load_policy(session: Session, policy_id: UUID) -> tuple[UUID, str]:
    policy = session.get(ApprovalPolicy, policy_id)
    if policy is None:
        raise ConnectError(f"policy {policy_id} does not exist.")
    problems = []
    if not policy.is_active:
        problems.append("it is inactive")
    if not policy.auto_dial:
        problems.append("auto-dial is off, so the guard would phone no one")
    found = (policy.organization_id, policy.name)
    session.rollback()  # end the read; writes start in their own transaction
    if problems:
        raise ConnectError(f"policy '{found[1]}' cannot be used: " + "; ".join(problems) + ".")
    return found


def _previous_key_id(session: Session, organization_id: UUID, plaintext: str) -> UUID | None:
    """The active guard key this project used before, if it is ours to revoke."""
    if not plaintext:
        return None
    key_id = session.scalar(
        select(ApiKey.id).where(
            ApiKey.key_hash == hash_api_key(plaintext),
            ApiKey.organization_id == organization_id,
            ApiKey.name == KEY_NAME,
            ApiKey.revoked_at.is_(None),
        )
    )
    session.rollback()
    return key_id


def connect(
    session: Session,
    *,
    project_dir: Path,
    policy_id: UUID,
    api_url: str,
    http: httpx.Client,
) -> ConnectResult:
    project_dir = project_dir.resolve()
    api_url = api_url.strip().rstrip("/")
    if not api_url.startswith(("http://", "https://")):
        raise ConnectError("--api-url must start with http:// or https://")
    if not project_dir.is_dir():
        raise ConnectError(f"{project_dir} is not a directory.")
    settings_file = local_config.settings_local_path(project_dir)
    rules_file = project_dir / RULES_FILE

    # 1. Checks: nothing is written or issued until all of them pass.
    _check_hook(project_dir)
    rules_text, rule_count = _rules_text(project_dir, policy_id)
    _require_ignored(settings_file)
    _require_ignored(rules_file)
    try:
        existing = local_config.read_settings_local(project_dir)
    except local_config.LocalSettingsError as exc:
        raise ConnectError(f"{exc.message} Fix or delete it, then run this again.") from None
    _check_api(http, api_url)
    organization_id, policy_name = _load_policy(session, policy_id)
    previous_key_id = _previous_key_id(
        session, organization_id, existing.get("env", {}).get(local_config.ENV_API_KEY, "")
    )

    # 2. Issue the key, then write both files. A failed write revokes the new key.
    with transaction(session):
        issued = api_key_service.issue_api_key(
            session, organization_id, KEY_NAME, list(GUARD_SCOPES)
        )
    env = {
        **existing.get("env", {}),
        local_config.ENV_API_URL: api_url,
        local_config.ENV_API_KEY: issued.plaintext,
        local_config.ENV_RULES: str(rules_file),
    }
    try:
        rules_file.write_text(rules_text, encoding="utf-8")
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(
            json.dumps({**existing, "env": env}, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        with transaction(session):
            api_key_service.revoke_api_key(session, issued.id)
        raise ConnectError(
            f"could not write the project files ({type(exc).__name__}); the new key was revoked."
        ) from None

    # 3. The old guard key is no longer referenced anywhere: revoke it.
    if previous_key_id is not None and previous_key_id != issued.id:
        with transaction(session):
            api_key_service.revoke_api_key(session, previous_key_id)

    return ConnectResult(
        project_dir=project_dir,
        settings_file=settings_file,
        rules_file=rules_file,
        rule_count=rule_count,
        policy_id=policy_id,
        policy_name=policy_name,
        organization_id=organization_id,
        key_id=issued.id,
        revoked_key_id=previous_key_id,
    )


def describe(result: ConnectResult) -> list[str]:
    """What happened, for the terminal. Never includes the key."""
    lines = [
        f"Connected Claude Code in {result.project_dir} to policy '{result.policy_name}' "
        f"({result.policy_id}).",
        f"  agent key  '{KEY_NAME}' (id {result.key_id}), scopes {', '.join(GUARD_SCOPES)}; "
        "stored only in settings.local.json, not shown",
    ]
    if result.revoked_key_id is not None:
        lines.append(f"  revoked    the previous guard key (id {result.revoked_key_id})")
    lines += [
        f"  wrote      {result.settings_file} (env: {', '.join(local_config.AGENT_VARS)})",
        f"  wrote      {result.rules_file} ({result.rule_count} rules)",
        "Next: restart Claude Code in that folder (it reads settings.local.json at start).",
    ]
    return lines
