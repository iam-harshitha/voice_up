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
"""

import argparse
import getpass
import os
import sys
from collections.abc import Sequence
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
    return parser


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


def _run(args: argparse.Namespace) -> None:
    try:
        settings = get_settings()
    except ValidationError as exc:
        raise CliError(
            f"invalid configuration (check DATABASE_URL): {exc.errors()[0]['msg']}"
        ) from exc
    if args.command == "voice-simulate":
        _voice_simulate(args, settings)
        return
    engine = build_engine(settings)
    session = build_session_factory(engine)()
    try:
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


def _print_warning() -> None:
    print(
        "\nStore these keys now (e.g. in your local, git-ignored .env). "
        "They are not stored and cannot be shown again.",
        file=sys.stderr,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        _run(args)
    except (CliError, ValueError, AppError) as exc:
        print(f"error: {getattr(exc, 'message', exc)}", file=sys.stderr)
        return 2
    except SQLAlchemyError as exc:
        print(f"error: database operation failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
