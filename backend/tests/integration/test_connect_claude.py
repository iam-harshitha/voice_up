"""``app.cli connect-claude``: writes the guard's settings safely, or nothing at all."""

import argparse
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from app import cli
from app.connect_claude import GUARD_SCOPES, KEY_NAME, ConnectError, connect
from app.core.security import hash_api_key
from app.db.models import ApiKey
from approval_line_agents import guard, local_config
from tests.helpers import create_approver, make_policy

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[3]
SHIPPED = REPO / "demo-target"
IGNORES = "demo-target/.claude/settings.local.json\ndemo-target/guard-rules.json\n"


def make_repo(tmp_path: Path, ignores: str | None = IGNORES) -> Path:
    """A git repository with a demo-target-like project (its own repo: hermetic)."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603, S607
    project = tmp_path / "demo-target"
    (project / ".claude").mkdir(parents=True)
    shutil.copy(SHIPPED / ".claude" / "settings.json", project / ".claude" / "settings.json")
    shutil.copy(SHIPPED / "guard-rules.example.json", project / "guard-rules.example.json")
    if ignores is not None:
        (tmp_path / ".gitignore").write_text(ignores, encoding="utf-8")
    return project


def api(status: int = 200) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/health/ready"
        return httpx.Response(status, json={"status": "ok"})

    return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.fixture
def setup(client, make_tenant, tmp_path):
    tenant = make_tenant()
    approver = create_approver(client, tenant, "Alice", phone_number="+14155550193")
    policy = make_policy(client, tenant, [approver["id"]], name="Deploys", auto_dial=True)
    return {
        "tenant": tenant,
        "policy": policy,
        "approver": approver,
        "project": make_repo(tmp_path),
    }


def run_connect(session, project, policy_id, status=200):
    return connect(
        session,
        project_dir=project,
        policy_id=policy_id,
        api_url="http://127.0.0.1:8000/",
        http=api(status),
    )


def key_count(session) -> int:
    count = session.scalar(select(func.count()).select_from(ApiKey))
    session.rollback()
    return count


def written_env(project: Path) -> dict:
    return local_config.read_settings_local(project)["env"]


def test_connect_writes_ignored_files_with_a_least_privilege_key(session, setup):
    project, policy = setup["project"], setup["policy"]
    settings_file = local_config.settings_local_path(project)
    settings_file.write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "env": {"OTHER": "kept"}}), "utf-8"
    )
    result = run_connect(session, project, policy["id"])

    env = written_env(project)
    assert env["OTHER"] == "kept"  # other settings survive
    assert local_config.read_settings_local(project)["permissions"] == {"allow": ["Bash(ls)"]}
    assert env[local_config.ENV_API_URL] == "http://127.0.0.1:8000"
    assert Path(env[local_config.ENV_RULES]) == project / "guard-rules.json"

    key = session.scalar(
        select(ApiKey).where(ApiKey.key_hash == hash_api_key(env[local_config.ENV_API_KEY]))
    )
    assert key is not None and key.id == result.key_id and key.name == KEY_NAME
    assert sorted(key.scopes) == sorted(GUARD_SCOPES)  # least privilege: no votes, no calls
    assert key.organization_id == setup["tenant"].organization_id

    rules_text = (project / "guard-rules.json").read_text(encoding="utf-8")
    assert "<" not in rules_text
    rules = guard.load_rules(env[local_config.ENV_RULES])
    assert {str(r.rule.policy_id) for r in rules} == {policy["id"]}
    for path in (settings_file, project / "guard-rules.json"):
        assert local_config.is_git_ignored(path) is True


def test_running_it_again_rotates_the_key(session, setup):
    project, policy_id = setup["project"], setup["policy"]["id"]
    first = run_connect(session, project, policy_id)
    old_key = written_env(project)[local_config.ENV_API_KEY]
    second = run_connect(session, project, policy_id)
    new_key = written_env(project)[local_config.ENV_API_KEY]

    assert new_key != old_key and second.revoked_key_id == first.key_id
    old = session.get(ApiKey, first.key_id)
    assert old.revoked_at is not None
    assert session.get(ApiKey, second.key_id).revoked_at is None


def assert_refused(session, setup, expected: str, *, status=200, policy_id=None):
    project = setup["project"]
    before = key_count(session)
    with pytest.raises(ConnectError) as caught:
        run_connect(session, project, policy_id or setup["policy"]["id"], status)
    assert expected in caught.value.message
    assert key_count(session) == before  # no key issued
    assert not (project / "guard-rules.json").exists()
    return caught.value.message


def test_refuses_when_the_files_are_not_git_ignored(session, setup, tmp_path):
    (tmp_path / ".gitignore").write_text("", encoding="utf-8")
    message = assert_refused(session, setup, "is not git-ignored")
    assert "settings.local.json" in message
    assert not local_config.settings_local_path(setup["project"]).exists()


def test_refuses_when_only_the_rules_file_is_not_ignored(session, setup, tmp_path):
    (tmp_path / ".gitignore").write_text(
        "demo-target/.claude/settings.local.json\n", encoding="utf-8"
    )
    assert_refused(session, setup, "guard-rules.json is not git-ignored")


def test_refuses_when_the_api_is_not_ready(session, setup):
    assert_refused(session, setup, "not ready (HTTP 503)", status=503)


def test_refuses_an_unknown_policy(session, setup):
    assert_refused(
        session, setup, "does not exist", policy_id="00000000-0000-4000-8000-000000000000"
    )


def test_refuses_a_policy_with_auto_dial_off(client, session, setup):
    tenant = setup["tenant"]
    response = client.patch(
        f"/api/v1/policies/{setup['policy']['id']}",
        json={"auto_dial": False},
        headers=tenant.admin,
    )
    assert response.status_code == 200
    assert_refused(session, setup, "auto-dial is off")


def test_refuses_a_project_without_the_guard_hook(session, setup):
    (setup["project"] / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    assert_refused(session, setup, "does not register the Approval Line guard")


def test_refuses_to_overwrite_an_unreadable_settings_file(session, setup):
    settings_file = local_config.settings_local_path(setup["project"])
    settings_file.write_text("{not json", encoding="utf-8")
    assert_refused(session, setup, "is not valid JSON")
    assert settings_file.read_text(encoding="utf-8") == "{not json"  # left untouched


def test_a_failed_write_revokes_the_new_key(session, setup):
    project = setup["project"]
    (project / "guard-rules.json").mkdir()  # a directory: writing the file fails
    with pytest.raises(ConnectError) as caught:
        run_connect(session, project, setup["policy"]["id"])
    assert "the new key was revoked" in caught.value.message
    guard_keys = session.scalars(select(ApiKey).where(ApiKey.name == KEY_NAME)).all()
    assert guard_keys and all(k.revoked_at is not None for k in guard_keys)
    assert not local_config.settings_local_path(project).exists()


def test_the_cli_never_prints_the_key(session, setup, capsys):
    args = argparse.Namespace(
        project=str(setup["project"]),
        policy_id=setup["policy"]["id"],
        api_url="http://127.0.0.1:8000",
        no_doctor=True,
        no_bolna=True,
    )
    assert cli._connect_claude(args, session, http=api()) == 0
    out = capsys.readouterr()
    key = written_env(setup["project"])[local_config.ENV_API_KEY]
    assert key not in out.out and key not in out.err
    assert "not shown" in out.out and "restart Claude Code" in out.out


def test_cli_refusal_is_an_error_exit(session, setup, tmp_path, capsys, monkeypatch):
    (tmp_path / ".gitignore").write_text("", encoding="utf-8")
    monkeypatch.setattr(cli, "_run", lambda args: cli._connect_claude(args, session, api()))
    code = cli.main(
        [
            "connect-claude",
            "--project",
            str(setup["project"]),
            "--policy-id",
            setup["policy"]["id"],
            "--no-doctor",
        ]
    )
    assert code == 2
    assert "not git-ignored" in capsys.readouterr().err
