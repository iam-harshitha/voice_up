"""Operator CLI for bootstrapping organizations and API keys.

Plaintext keys are written to stdout exactly once and are never logged or stored.

Examples:
    python -m app.cli create-org --name "Acme" \\
        --key "agent=requests:create,requests:read" \\
        --key "admin=policies:read,policies:write,approvers:read,approvers:write,requests:read"
    python -m app.cli create-key --org-id <uuid> --name voter --scopes votes:write
    python -m app.cli list-keys --org-id <uuid>
    python -m app.cli revoke-key --key-id <uuid>
    python -m app.cli create-dashboard-user --org-id <uuid> --email ops@example.com \\
        --name "Ops Lead" --scopes "requests:read,requests:create,policies:read,..."
    python -m app.cli disable-dashboard-user --email ops@example.com
    python -m app.cli demo-seed --org-id <uuid> --yes [--issue-agent-key]
    python -m app.cli connect-claude --project demo-target --policy-id <uuid>
    python -m app.cli pair-telegram --approver-id <uuid>
    python -m app.cli set-channel --policy-id <uuid> --channel text
    python -m app.cli text-simulate --prompt-id <uuid> --action approve   # mock only
"""

import argparse
import getpass
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import AGENT_SCOPES, ALL_SCOPES
from app.db.session import build_engine, build_session_factory, transaction
from app.services import api_key_service, dashboard_auth_service
from app.services.api_key_service import IssuedApiKey

_SCOPE_HELP = (
    f"Comma-separated scopes. Valid: {', '.join(sorted(ALL_SCOPES))}. "
    f"A typical agent key needs only: {','.join(sorted(AGENT_SCOPES))}."
)


class CliError(Exception):
    pass


def _key_spec(value: str) -> tuple[str, str]:
    name, sep, scopes = value.partition("=")
    if not sep or not name.strip() or not scopes.strip():
        raise argparse.ArgumentTypeError("expected NAME=SCOPE[,SCOPE...]")
    return name.strip(), scopes


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="approval-line",
        description="Bootstrap organizations and scoped API keys for Approval Line.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create_org = commands.add_parser("create-org", help="Create an organization (and keys).")
    create_org.add_argument("--name", required=True, help="Organization name.")
    create_org.add_argument(
        "--key",
        dest="keys",
        action="append",
        type=_key_spec,
        default=[],
        metavar="NAME=SCOPES",
        help=f"Issue a key; repeatable. {_SCOPE_HELP}",
    )

    create_key = commands.add_parser("create-key", help="Issue a key for an organization.")
    create_key.add_argument("--org-id", required=True, type=UUID)
    create_key.add_argument("--name", required=True, help="Human-readable key label.")
    create_key.add_argument("--scopes", required=True, help=_SCOPE_HELP)

    list_keys = commands.add_parser("list-keys", help="List an organization's keys (no secrets).")
    list_keys.add_argument("--org-id", required=True, type=UUID)

    revoke = commands.add_parser("revoke-key", help="Revoke a key immediately.")
    revoke.add_argument("--key-id", required=True, type=UUID)

    dash = commands.add_parser(
        "create-dashboard-user",
        help="Create a dashboard (browser) user with a password and scoped access.",
        description=(
            "The password is read from an interactive prompt, or from the environment "
            "variable named by --password-env (never from the command line). votes:write "
            "cannot be granted to dashboard users."
        ),
    )
    dash.add_argument("--org-id", required=True, type=UUID)
    dash.add_argument("--email", required=True)
    dash.add_argument("--name", required=True, help="Display name.")
    dash.add_argument("--scopes", required=True, help=_SCOPE_HELP)
    dash.add_argument(
        "--password-env",
        metavar="VAR",
        help="Read the password from this environment variable (for scripted setup).",
    )

    disable = commands.add_parser(
        "disable-dashboard-user",
        help="Deactivate a dashboard user and revoke its sessions and backing key.",
    )
    disable.add_argument("--email", required=True)

    demo = commands.add_parser(
        "demo-seed",
        help="DEMO ONLY: add the demo approvers and policies to an existing organization.",
        description=(
            "Idempotent and additive: creates fictional approvers (555-01xx numbers) and two "
            "demo policies in the given organization, reusing identical ones and refusing to "
            "touch conflicting ones. Never creates requests or votes, never deletes anything. "
            "Refused when APP_ENV=production. Requires --yes."
        ),
    )
    demo.add_argument("--org-id", required=True, type=UUID)
    demo.add_argument("--yes", action="store_true", help="Confirm the additive changes.")
    demo.add_argument(
        "--issue-agent-key",
        action="store_true",
        help="Also issue a 'demo-agent' key (requests:create,requests:read,audit:read,"
        "policies:read), printed once. Skipped if an active one already exists.",
    )

    simulate = commands.add_parser(
        "voice-simulate",
        help="DEVELOPMENT ONLY: finish a mock call and deliver its webhook to a running API.",
        description=(
            "Requires VOICE_PROVIDER=mock, VOICE_MOCK_STATE_DIR and a non-production APP_ENV. "
            "Completes the simulated Bolna execution for a voice call and POSTs it to the "
            "API's real, token-authenticated webhook endpoint."
        ),
    )
    simulate.add_argument("--call-id", required=True, type=UUID, help="Voice call ID.")
    simulate.add_argument(
        "--decision",
        default="approve",
        help=(
            "Extracted decision: approve, reject, uncertain, needs_clarification, "
            "conditional_approve, or none."
        ),
    )
    simulate.add_argument("--condition", help="Stated condition (with conditional_approve).")
    simulate.add_argument("--identity", default="yes", help="Identity confirmed: yes/no/none.")
    simulate.add_argument("--confidence", type=float, default=0.95)
    simulate.add_argument(
        "--status", default="completed", help="Final Bolna status (e.g. completed, no-answer)."
    )
    simulate.add_argument("--api-url", default="http://127.0.0.1:8000")

    connect = commands.add_parser(
        "connect-claude",
        help="Connect a Claude Code project (e.g. demo-target) to a policy: no env vars needed.",
        description=(
            "Checks the API, issues a guard key (requests:create, requests:read, "
            "policies:read), writes <project>/.claude/settings.local.json and "
            "<project>/guard-rules.json (both must be git-ignored), revokes the project's "
            "previous guard key, then runs the demo doctor. The key is never printed."
        ),
    )
    connect.add_argument(
        "--project",
        default="demo-target",
        help="Claude Code project folder; a relative path is tried from here, then from "
        "the repository root (default: demo-target).",
    )
    connect.add_argument("--policy-id", required=True, type=UUID)
    connect.add_argument("--api-url", default="http://127.0.0.1:8000")
    connect.add_argument("--no-doctor", action="store_true", help="Skip the doctor run.")
    connect.add_argument("--no-bolna", action="store_true", help="Doctor: skip the Bolna GET.")

    pair = commands.add_parser(
        "pair-telegram",
        help="Print a one-time Telegram link (valid 10 minutes) that links an approver.",
        description=(
            "Needs TEXT_PROVIDER=telegram (or mock). Open the printed t.me link on the "
            "approver's phone and press Start; the bot replies with the approver's name. "
            "Earlier unused links stop working; using the new one replaces any old link."
        ),
    )
    pair.add_argument("--approver-id", required=True, type=UUID)

    channel = commands.add_parser(
        "set-channel",
        help="Switch a policy's approval_channel (voice or text). Audited; bumps the version.",
    )
    channel.add_argument("--policy-id", required=True, type=UUID)
    channel.add_argument("--channel", required=True, choices=["voice", "text"])

    text_sim = commands.add_parser(
        "text-simulate",
        help="DEVELOPMENT ONLY: simulate a Telegram tap or message for the text worker.",
        description=(
            "Requires TEXT_PROVIDER=mock, TEXT_MOCK_STATE_DIR and a non-production APP_ENV. "
            "Queues a Telegram-shaped update that a running text worker then handles "
            "through the real checks. Use exactly one of --prompt-id, --start-url, --text "
            "or --outbox."
        ),
    )
    target = text_sim.add_mutually_exclusive_group(required=True)
    target.add_argument("--prompt-id", type=UUID, help="Tap a button on this prompt's message.")
    target.add_argument("--start-url", help="Press Start on a pairing link (t.me/...?start=...).")
    target.add_argument("--text", help="Send a free-text message to the bot.")
    target.add_argument("--outbox", action="store_true", help="Show what the bot sent.")
    text_sim.add_argument(
        "--action", choices=["approve", "reject", "confirm", "cancel"], default="approve"
    )
    text_sim.add_argument(
        "--from-user",
        help="Telegram user ID of the sender (default: the prompt's linked account; "
        "required with --start-url and --text). Fictional IDs only.",
    )
    return parser


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _project_dir(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or path.exists():
        return path
    return BACKEND_DIR.parent / path


def _connect_claude(args: argparse.Namespace, session, http=None) -> int:
    import httpx

    from app.connect_claude import ConnectError, connect, describe

    project = _project_dir(args.project)
    try:
        with httpx.Client(follow_redirects=False) if http is None else http as client:
            result = connect(
                session,
                project_dir=project,
                policy_id=args.policy_id,
                api_url=args.api_url,
                http=client,
            )
    except ConnectError as exc:
        raise CliError(exc.message) from None
    print("\n".join(describe(result)))
    if args.no_doctor:
        return 0
    print("\nRunning the demo doctor (read-only) for this project...\n", flush=True)
    argv = [sys.executable, str(BACKEND_DIR / "scripts" / "demo_doctor.py")]
    argv += ["--project", str(result.project_dir)] + (["--no-bolna"] if args.no_bolna else [])
    return subprocess.run(argv, cwd=BACKEND_DIR, check=False).returncode  # noqa: S603


def _voice_simulate(args: argparse.Namespace, settings) -> None:
    import httpx

    from app.api.v1.endpoints.voice_webhooks import WEBHOOK_TOKEN_HEADER
    from app.voice.mock import MockVoiceProvider

    if (
        settings.voice_provider != "mock"
        or settings.app_env == "production"
        or not settings.voice_mock_state_dir
        or settings.voice_webhook_token is None
    ):
        raise CliError(
            "voice-simulate needs VOICE_PROVIDER=mock, VOICE_MOCK_STATE_DIR, "
            "VOICE_WEBHOOK_TOKEN and a non-production APP_ENV"
        )
    mock = MockVoiceProvider(settings)
    execution = mock.execution_for(str(args.call_id))
    if execution is None:
        raise CliError("no simulated execution for that call (has the worker dialled it?)")

    def optional(value: str) -> str | None:
        return None if value.lower() == "none" else value

    if args.status == "completed":
        payload = mock.complete(
            execution["id"],
            decision=optional(args.decision),
            identity=optional(args.identity),
            confidence=args.confidence,
            identity_confidence=args.confidence,
            condition=args.condition,
        )
    else:
        payload = mock.set_status(execution["id"], args.status)
    response = httpx.post(
        f"{args.api_url.rstrip('/')}{settings.api_v1_prefix}/voice/webhooks/bolna",
        json=payload,
        headers={WEBHOOK_TOKEN_HEADER: settings.voice_webhook_token.get_secret_value()},
        timeout=10,
    )
    print(f"webhook delivered: HTTP {response.status_code} {response.text}")
    if response.status_code != 200:
        raise CliError("the API did not accept the simulated webhook")


def _check_bot_username(settings, telegram=None) -> None:
    """Refuse to print a pairing link that would open another Telegram account."""
    from app.text.base import TextProviderError
    from app.text.telegram import TelegramProvider, username_mismatch

    provider = telegram or TelegramProvider(settings)
    try:
        actual = provider.get_me()
    except TextProviderError as exc:
        raise CliError(
            f"could not confirm the bot with Telegram (getMe: {exc.code}); check "
            "TELEGRAM_BOT_TOKEN and the network"
        ) from None
    finally:
        provider.close()
    problem = username_mismatch(settings.telegram_bot_username, actual)
    if problem:
        raise CliError(problem)


def _pair_telegram(args: argparse.Namespace, settings, session, telegram=None) -> None:
    from sqlalchemy import select

    from app.db.models import Approver
    from app.services import channel_link_service

    approver = session.scalar(select(Approver).where(Approver.id == args.approver_id))
    session.rollback()
    if approver is None:
        raise CliError("approver not found")
    if settings.text_provider == "telegram":
        _check_bot_username(settings, telegram)
    try:
        pairing = channel_link_service.start_pairing(
            session, settings, approver.organization_id, approver.id
        )
    except AppError as exc:
        raise CliError(exc.message) from exc
    expires = pairing.link.pairing_expires_at
    print(f"Telegram pairing link for '{approver.name}' (one-time, expires {expires:%H:%M} UTC):")
    print(f"  {pairing.url}")
    print("Open it on the approver's phone and press Start. The bot replies with their name.")
    print("Whoever opens this link first is linked, so send it only to the approver.")


def _set_channel(args: argparse.Namespace, session) -> None:
    from sqlalchemy import select

    from app.db.models import ApprovalPolicy
    from app.schemas.policy import PolicyUpdate
    from app.services import policy_service

    org_id = session.scalar(
        select(ApprovalPolicy.organization_id).where(ApprovalPolicy.id == args.policy_id)
    )
    session.rollback()
    if org_id is None:
        raise CliError("policy not found")
    try:
        policy = policy_service.update_policy(
            session, org_id, args.policy_id, PolicyUpdate(approval_channel=args.channel)
        )
    except AppError as exc:
        raise CliError(exc.message) from exc
    print(
        f"Policy '{policy.name}': approval_channel={policy.approval_channel}, "
        f"auto_dial={'on' if policy.auto_dial else 'off'}, version {policy.version}"
    )
    if policy.approval_channel == "text":
        print("New requests under it are sent by Telegram only; no call is placed.")


_TAP_ACTIONS = {"approve": "a", "reject": "r", "confirm": "c", "cancel": "x"}


def _text_simulate(args: argparse.Namespace, settings, session) -> None:
    from urllib.parse import parse_qs, urlparse

    from app.db.models import ApproverChannelLink, TextPrompt
    from app.text.mock import MockTextProvider

    if (
        settings.text_provider != "mock"
        or settings.app_env == "production"
        or not settings.text_mock_state_dir
    ):
        raise CliError(
            "text-simulate needs TEXT_PROVIDER=mock, TEXT_MOCK_STATE_DIR and a "
            "non-production APP_ENV"
        )
    mock = MockTextProvider(settings)
    if args.outbox:
        for item in mock.outbox()[-10:]:
            print(f"[{item['kind']}] {item.get('text', '')}\n")
        return
    if args.prompt_id is not None:
        prompt = session.get(TextPrompt, args.prompt_id)
        if prompt is None or prompt.provider_message_id is None:
            raise CliError("no delivered prompt with that ID (is the text worker running?)")
        sender = args.from_user
        if sender is None:
            link = session.get(ApproverChannelLink, prompt.link_id)
            sender = link.external_user_id if link is not None else None
        if not sender:
            raise CliError("--from-user is required (the prompt's link has no account)")
        try:
            mock.tap_button(prompt.provider_message_id, _TAP_ACTIONS[args.action], sender_id=sender)
        except LookupError as exc:
            raise CliError(f"no '{args.action}' button on the message right now") from exc
        print(f"tap queued: {args.action} on prompt {prompt.id}; the text worker handles it")
        return
    if not args.from_user or not args.from_user.isdigit():
        raise CliError("--from-user must be a numeric (fictional) Telegram user ID")
    if args.start_url is not None:
        code = parse_qs(urlparse(args.start_url).query).get("start", [""])[0]
        mock.send_text(f"/start {code}", sender_id=args.from_user)
        print("Start queued; the text worker links the account and replies")
    else:
        mock.send_text(args.text, sender_id=args.from_user)
        print("message queued (free text is never counted as a decision)")


DEMO_AGENT_KEY = "demo-agent"
DEMO_AGENT_SCOPES = "requests:create,requests:read,audit:read,policies:read"


def _demo_seed(args: argparse.Namespace, settings, session) -> None:
    from app.services import demo_seed_service

    if settings.app_env == "production":
        raise CliError("demo-seed is refused when APP_ENV=production")
    if not args.yes:
        raise CliError(
            "demo-seed adds demo approvers and policies to organization "
            f"{args.org_id}. Re-run with --yes to confirm."
        )
    try:
        result = demo_seed_service.seed(session, args.org_id)
    except demo_seed_service.DemoSeedError as exc:
        raise CliError(str(exc)) from exc
    print(f"Demo data in organization '{result.organization_name}' ({args.org_id}):")
    for line in result.created:
        print(f"  created  {line}")
    for line in result.reused:
        print(f"  reused   {line}")
    for name, policy_id in result.policies.items():
        print(f"  policy id  {policy_id}  {name}")
    if args.issue_agent_key:
        active = [
            k
            for k in api_key_service.list_api_keys(session, args.org_id)
            if k.name == DEMO_AGENT_KEY and k.revoked_at is None
        ]
        session.rollback()
        if active:
            print(
                f"  agent key '{DEMO_AGENT_KEY}' already active (id {active[0].id}); not reissued"
            )
        else:
            with transaction(session):
                key = api_key_service.issue_api_key(
                    session, args.org_id, DEMO_AGENT_KEY, DEMO_AGENT_SCOPES
                )
            _print_issued(key)
            _print_warning()


def _read_password(args: argparse.Namespace) -> str:
    if args.password_env:
        value = os.environ.get(args.password_env)
        if not value:
            raise CliError(f"environment variable {args.password_env} is not set")
        return value
    first = getpass.getpass("Password (min 12 characters): ")
    if first != getpass.getpass("Repeat password: "):
        raise CliError("passwords do not match")
    return first


def _print_issued(key: IssuedApiKey) -> None:
    print(f"API key '{key.name}' (id {key.id})")
    print(f"  scopes: {', '.join(key.scopes)}")
    print(f"  key:    {key.plaintext}")


def _run(args: argparse.Namespace) -> int:
    try:
        settings = get_settings()
    except ValidationError as exc:
        raise CliError(
            f"invalid configuration (check DATABASE_URL): {exc.errors()[0]['msg']}"
        ) from exc
    if args.command == "voice-simulate":
        _voice_simulate(args, settings)
        return 0
    engine = build_engine(settings)
    session = build_session_factory(engine)()
    try:
        if args.command == "connect-claude":
            return _connect_claude(args, session)
        if args.command == "pair-telegram":
            _pair_telegram(args, settings, session)
            return 0
        if args.command == "set-channel":
            _set_channel(args, session)
            return 0
        if args.command == "text-simulate":
            _text_simulate(args, settings, session)
            return 0
        if args.command == "create-org":
            with transaction(session):
                organization = api_key_service.create_organization(session, args.name)
                issued = [
                    api_key_service.issue_api_key(session, organization.id, name, scopes)
                    for name, scopes in args.keys
                ]
            print(f"Organization '{organization.name}' created (id {organization.id})")
            for key in issued:
                _print_issued(key)
            if issued:
                _print_warning()
        elif args.command == "create-key":
            with transaction(session):
                key = api_key_service.issue_api_key(session, args.org_id, args.name, args.scopes)
            _print_issued(key)
            _print_warning()
        elif args.command == "list-keys":
            for key in api_key_service.list_api_keys(session, args.org_id):
                state = f"revoked {key.revoked_at.isoformat()}" if key.revoked_at else "active"
                print(f"{key.id}  {key.name:<24} {state:<34} {','.join(key.scopes)}")
        elif args.command == "create-dashboard-user":
            password = _read_password(args)
            with transaction(session):
                user = dashboard_auth_service.create_user(
                    session,
                    args.org_id,
                    email=args.email,
                    display_name=args.name,
                    password=password,
                    scopes=args.scopes,
                )
            print(f"Dashboard user '{user.email}' created (id {user.id})")
        elif args.command == "disable-dashboard-user":
            with transaction(session):
                user = dashboard_auth_service.disable_user(session, args.email)
            print(f"Dashboard user '{user.email}' disabled; sessions and backing key revoked")
        elif args.command == "demo-seed":
            _demo_seed(args, settings, session)
        elif args.command == "revoke-key":
            with transaction(session):
                key = api_key_service.revoke_api_key(session, args.key_id)
            print(f"API key {key.id} revoked at {key.revoked_at.isoformat()}")
    finally:
        session.close()
        engine.dispose()
    return 0


def _print_warning() -> None:
    print(
        "\nStore these keys now (e.g. in your local, git-ignored .env). "
        "They are not stored and cannot be shown again.",
        file=sys.stderr,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _run(args)
    except (CliError, ValueError, AppError) as exc:
        print(f"error: {getattr(exc, 'message', exc)}", file=sys.stderr)
        return 2
    except SQLAlchemyError as exc:
        print(f"error: database operation failed ({type(exc).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
