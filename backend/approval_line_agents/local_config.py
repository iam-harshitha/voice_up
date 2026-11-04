"""A project's Claude Code ``.claude/settings.local.json`` "env" block, as the guard sees it.

Claude Code applies that block to the session and passes it to hooks and MCP servers, and
it takes precedence over the variables of the window Claude Code was started in (verified
on Claude Code 2.1.316). The demo doctor and ``app.cli connect-claude`` use this module so
they check and write exactly the values the hook will receive. The guard itself does not
read the file: Claude Code hands it the values.

The file holds the agent key, so it must be git-ignored; ``is_git_ignored`` checks that.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

ENV_API_URL = "APPROVAL_LINE_API_URL"
ENV_API_KEY = "APPROVAL_LINE_API_KEY"
ENV_RULES = "APPROVAL_LINE_GUARD_RULES"
AGENT_VARS = (ENV_API_URL, ENV_API_KEY, ENV_RULES)

SOURCE_SETTINGS = "settings.local.json"
SOURCE_PROCESS = "process environment"


class LocalSettingsError(Exception):
    """The settings file exists but cannot be used. ``message`` never contains values."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def settings_local_path(project_dir: Path) -> Path:
    return project_dir / ".claude" / "settings.local.json"


def read_settings_local(project_dir: Path) -> dict:
    """The whole settings object; ``{}`` if the file does not exist."""
    path = settings_local_path(project_dir)
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except OSError:
        raise LocalSettingsError(f"{path} cannot be read.") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise LocalSettingsError(f"{path} is not valid JSON.") from None
    if not isinstance(data, dict):
        raise LocalSettingsError(f"{path} must contain a JSON object.")
    env = data.get("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise LocalSettingsError(f'{path}: "env" must map names to string values.')
    return data


def effective_env(
    process_env: Mapping[str, str], project_dir: Path
) -> tuple[dict[str, str], dict[str, str]]:
    """(environment the hook sees, source of each agent variable that is set).

    The settings file wins over the process environment, as in Claude Code.
    """
    file_env = read_settings_local(project_dir).get("env", {})
    merged = {**process_env, **file_env}
    sources = {
        name: SOURCE_SETTINGS if name in file_env else SOURCE_PROCESS
        for name in AGENT_VARS
        if merged.get(name, "").strip()
    }
    return merged, sources


def is_git_ignored(path: Path) -> bool | None:
    """True/False from ``git check-ignore``; None if git or the repository is unavailable.

    A tracked file is reported as not ignored, which is what matters for a secret.
    """
    base = path.parent
    while not base.is_dir() and base != base.parent:  # e.g. .claude/ not created yet
        base = base.parent
    argv = ["git", "-C", str(base), "check-ignore", "-q", "--", path.relative_to(base).as_posix()]
    try:
        # Fixed argument list, no shell; "git" from PATH, like the doctor's other probes.
        result = subprocess.run(argv, capture_output=True, timeout=15, check=False)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None
