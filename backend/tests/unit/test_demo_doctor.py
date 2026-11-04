"""Pure checks of the read-only demo doctor (no database, no network)."""

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "demo_doctor", BACKEND / "scripts" / "demo_doctor.py"
)
doctor = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = doctor  # dataclasses resolve their module by name
_spec.loader.exec_module(doctor)


@pytest.mark.parametrize(
    ("output", "status"),
    [
        ("2.1.295 (Claude Code)", "PASS"),
        ("2.2.0 (Claude Code)", "PASS"),
        ("3.0.1", "PASS"),
        ("2.1.294 (Claude Code)", "FAIL"),
        ("2.1.109 (Claude Code)", "FAIL"),
        ("garbage", "FAIL"),
        (None, "FAIL"),
    ],
)
def test_claude_version(output, status):
    assert doctor.check_claude_version(output).status == status


def hook_settings(**hook):
    base = {
        "type": "command",
        "command": "${CLAUDE_PROJECT_DIR}/python.exe",
        "args": ["-m", "approval_line_agents.guard"],
        "timeout": 420,
        "onFailure": "block",
    }
    return {"hooks": {"PreToolUse": [{"matcher": "Bash|PowerShell", "hooks": [{**base, **hook}]}]}}


def test_hook_settings_pass_and_resolve_the_interpreter(tmp_path):
    (tmp_path / "python.exe").write_text("")
    checks, python = doctor.check_hook_settings(hook_settings(), tmp_path, 300)
    assert [c.status for c in checks] == ["PASS", "PASS"]
    assert python == (tmp_path / "python.exe").resolve()


@pytest.mark.parametrize(
    ("overrides", "matcher", "problem"),
    [
        ({"onFailure": "continue"}, "Bash|PowerShell", "onFailure"),
        ({"timeout": 300}, "Bash|PowerShell", "timeout"),
        ({}, "Bash", "PowerShell"),
    ],
)
def test_hook_settings_problems(tmp_path, overrides, matcher, problem):
    settings = hook_settings(**overrides)
    settings["hooks"]["PreToolUse"][0]["matcher"] = matcher
    checks, _ = doctor.check_hook_settings(settings, tmp_path, 300)
    assert checks[0].status == "FAIL" and problem in checks[0].detail


def test_missing_interpreter_fails(tmp_path):
    checks, python = doctor.check_hook_settings(hook_settings(), tmp_path, 300)
    assert python is None and checks[-1].status == "FAIL"


@pytest.mark.parametrize(
    ("scopes", "revoked", "status"),
    [
        (["requests:create", "requests:read", "policies:read"], False, "PASS"),
        (["requests:create", "requests:read"], False, "WARN"),
        (["requests:create", "requests:read", "policies:read", "votes:write"], False, "WARN"),
        (["requests:read"], False, "FAIL"),
        (["requests:create", "requests:read", "policies:read"], True, "FAIL"),
    ],
)
def test_scopes(scopes, revoked, status):
    assert doctor.check_scopes(scopes, revoked).status == status


def test_bolna_agent_comparison_never_shows_the_token():
    token = "t" * 40
    good = {
        "webhook_url": "https://x/h",
        "webhook_headers": {"X-Approval-Line-Webhook-Token": token},
    }
    assert [c.status for c in doctor.check_bolna_agent(good, "https://x/h", token)] == [
        "PASS",
        "PASS",
    ]
    bad = doctor.check_bolna_agent({"webhook_headers": {}}, "https://x/h", token, 30)
    # A stale webhook URL only slows decisions down (they arrive by polling): a warning.
    # A wrong token means a misconfigured agent: still a failure.
    assert [c.status for c in bad] == ["WARN", "FAIL"]
    assert "decisions still arrive by polling (every 30 s)" in bad[0].detail
    assert all(token not in c.detail for c in bad)


def test_polling_fallback_check():
    assert doctor.check_polling(30).status == "PASS"
    assert doctor.check_polling(60).status == "PASS"
    slow = doctor.check_polling(600)
    assert slow.status == "WARN" and "set 30" in slow.detail


def test_agent_settings_report_their_source_or_the_fix(tmp_path):
    settings_file = tmp_path / ".claude" / "settings.local.json"
    sources = {
        "APPROVAL_LINE_API_URL": "settings.local.json",
        "APPROVAL_LINE_API_KEY": "process environment",
    }
    checks = {c.name: c for c in doctor.check_agent_settings(sources, settings_file)}
    assert checks["APPROVAL_LINE_API_URL"].status == "PASS"
    assert "settings.local.json" in checks["APPROVAL_LINE_API_URL"].detail
    assert "process environment" in checks["APPROVAL_LINE_API_KEY"].detail
    missing = checks["APPROVAL_LINE_GUARD_RULES"]
    assert missing.status == "FAIL"
    assert "APPROVAL_LINE_GUARD_RULES is not set" in missing.detail
    assert "connect-claude" in missing.detail


def test_settings_file_ignore_check():
    assert doctor.check_settings_file_ignored(True).status == "PASS"
    assert doctor.check_settings_file_ignored(None).status == "WARN"
    assert doctor.check_settings_file_ignored(False).status == "FAIL"


def test_policy_row_checks():
    pid, org = uuid.uuid4(), uuid.uuid4()
    row = {"name": "P", "is_active": True, "auto_dial": True, "organization_id": org}
    ok = [{"is_active": True, "has_phone": True}]
    assert doctor.check_policy_row(pid, row, ok, org).status == "PASS"
    assert doctor.check_policy_row(pid, None, ok, org).status == "FAIL"
    assert doctor.check_policy_row(pid, {**row, "auto_dial": False}, ok, org).status == "FAIL"
    no_phone = [{"is_active": True, "has_phone": False}]
    assert doctor.check_policy_row(pid, row, no_phone, org).status == "FAIL"
    assert doctor.check_policy_row(pid, row, ok, uuid.uuid4()).status == "FAIL"


def test_verdict_and_rendering():
    report = doctor.Report()
    report.add("A", "PASS", "fine")
    report.add("B", "WARN", "hmm")
    assert report.ready and "READY" in doctor.render(report)
    report.add("C", "FAIL", "broken")
    assert not report.ready and "NOT READY" in doctor.render(report)


# --- Text approvals ------------------------------------------------------------------------


def _text_settings(**values):
    from types import SimpleNamespace

    from pydantic import SecretStr

    base = {
        "text_provider": "telegram",
        "telegram_bot_token": SecretStr("123456789:" + "U" * 35),
        "telegram_bot_username": "approval_line_test_bot",
        "text_callback_secret": SecretStr("u" * 40),
    }
    return SimpleNamespace(**{**base, **values})


def test_text_settings_check_reports_names_never_values():
    checks = doctor.check_text_settings(_text_settings())
    assert [c.status for c in checks] == ["PASS", "PASS"]
    assert "U" * 35 not in str(checks) and "u" * 40 not in str(checks)
    missing = doctor.check_text_settings(_text_settings(telegram_bot_token=None))
    assert missing[1].status == "FAIL" and "TELEGRAM_BOT_TOKEN" in missing[1].detail
    assert doctor.check_text_settings(_text_settings(text_provider="disabled"))[0].status == "SKIP"
    assert doctor.check_text_settings(_text_settings(text_provider="mock"))[0].status == "WARN"


def test_text_pairing_and_worker_checks():
    pid = uuid.uuid4()
    assert doctor.check_text_pairing(pid, "voice", "telegram", []) is None
    assert doctor.check_text_pairing(pid, "text", "disabled", [True]).status == "FAIL"
    assert doctor.check_text_pairing(pid, "text", "telegram", [True, False]).status == "FAIL"
    assert doctor.check_text_pairing(pid, "text", "telegram", []).status == "FAIL"
    assert doctor.check_text_pairing(pid, "text", "telegram", [True]).status == "PASS"
    assert doctor.check_text_worker(0, 0, text_needed=True).status == "FAIL"
    assert doctor.check_text_worker(0, 0, text_needed=False).status == "WARN"
    assert doctor.check_text_worker(1, 2, text_needed=True).status == "FAIL"
    assert doctor.check_text_worker(None, 0, text_needed=True).status == "WARN"
    assert doctor.check_text_worker(1, 0, text_needed=True).status == "PASS"


def test_a_text_policy_needs_no_phone_numbers():
    pid, org = uuid.uuid4(), uuid.uuid4()
    row = {"name": "T", "is_active": True, "auto_dial": True, "organization_id": org}
    no_phone = [{"is_active": True, "has_phone": False}]
    assert doctor.check_policy_row(pid, row, no_phone, org).status == "FAIL"
    text_row = {**row, "approval_channel": "text"}
    assert doctor.check_policy_row(pid, text_row, no_phone, org).status == "PASS"


def test_telegram_bot_check():
    assert doctor.check_telegram_bot("Ready57", "Ready57Bot", None).status == "FAIL"
    assert doctor.check_telegram_bot("Ready57Bot", "Ready57Bot", None).status == "PASS"
    assert doctor.check_telegram_bot("Ready57Bot", None, "ConnectError").status == "WARN"


def test_text_then_voice_pairing_problems_are_warnings():
    pid = uuid.uuid4()
    assert doctor.check_text_pairing(pid, "text_then_voice", "telegram", [False]).status == "WARN"
    assert doctor.check_text_pairing(pid, "text_then_voice", "disabled", [True]).status == "WARN"
    assert doctor.check_text_pairing(pid, "text_then_voice", "telegram", [True]).status == "PASS"


def test_voice_fallback_must_fit_the_guard_wait():
    pid = uuid.uuid4()
    ok = doctor.check_voice_fallback(pid, 10, 300.0)
    assert ok.status == "PASS" and "call after 10 s" in ok.detail
    too_long = doctor.check_voice_fallback(pid, 600, 300.0)
    assert (
        too_long.status == "WARN"
        and "APPROVAL_LINE_GUARD_TIMEOUT_SECONDS >= 840" in too_long.detail
    )
    assert doctor.check_voice_fallback(pid, 600, 900.0).status == "PASS"
