"""Dashboard sign-in: sessions, CSRF, lockout, revocation and tenant isolation."""

from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, update

from app.db.base import utcnow
from app.db.models import ApiKey, DashboardSession, DashboardUser
from app.db.session import transaction
from app.services import dashboard_auth_service
from tests.helpers import (
    API,
    DASHBOARD_PASSWORD,
    create_approver,
    create_dashboard_user,
    create_request,
    dashboard_login,
    make_policy,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def user(session_factory, tenant):
    user_id, key_id = create_dashboard_user(session_factory, tenant.organization_id)
    return {"id": user_id, "key_id": key_id}


@pytest.fixture
def pending(client, tenant):
    a = create_approver(client, tenant, "Alice")["id"]
    policy = make_policy(client, tenant, [a])
    return create_request(client, tenant, policy["id"])


def login(client, email="ops@example.com", password=DASHBOARD_PASSWORD):
    return client.post(f"{API}/auth/login", json={"email": email, "password": password})


def test_login_sets_a_hardened_cookie_and_returns_no_secrets(client, user):
    response = login(client)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{dashboard_auth_service.SESSION_COOKIE}=")
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=strict" in lowered and "path=/api/v1" in lowered
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["user"]["email"] == "ops@example.com"
    assert "votes:write" not in body["scopes"]
    assert body["environment"]["voice_provider"] == "disabled"
    token = client.cookies.get(dashboard_auth_service.SESSION_COOKIE)
    text = response.text
    for secret in (token, DASHBOARD_PASSWORD, "scrypt$"):
        assert secret not in text
    assert body["csrf_token"] == dashboard_auth_service.csrf_token_for(token)


def test_session_token_is_stored_only_as_a_hash(client, user, session):
    login(client)
    token = client.cookies.get(dashboard_auth_service.SESSION_COOKIE)
    stored = session.scalars(select(DashboardSession.token_hash)).all()
    assert stored == [dashboard_auth_service.hash_token(token)]
    assert token not in stored


def test_session_endpoint_requires_a_session(client, user):
    assert client.get(f"{API}/auth/session").status_code == 401
    login(client)
    response = client.get(f"{API}/auth/session")
    assert response.status_code == 200 and response.json()["user"]["display_name"] == "Ops Lead"


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("ops@example.com", "wrong password entirely"),
        ("nobody@example.com", DASHBOARD_PASSWORD),
        ("not-an-email", DASHBOARD_PASSWORD),
    ],
)
def test_failed_logins_are_indistinguishable(client, user, email, password):
    response = login(client, email, password)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "login_failed"
    assert dashboard_auth_service.SESSION_COOKIE not in response.headers.get("set-cookie", "")


def test_unauthenticated_dashboard_access_is_refused(client, pending):
    response = client.get(f"{API}/requests")
    assert response.status_code == 401


def test_session_reads_with_the_users_scopes(client, user, pending):
    dashboard_login(client)
    detail = client.get(f"{API}/requests/{pending['id']}")
    assert detail.status_code == 200 and detail.json()["id"] == pending["id"]


def test_missing_scope_is_403(client, session_factory, tenant, pending):
    create_dashboard_user(
        session_factory,
        tenant.organization_id,
        email="viewer@example.com",
        scopes=["requests:read"],
    )
    dashboard_login(client, "viewer@example.com")
    assert client.get(f"{API}/policies").status_code == 403


def test_state_changes_require_the_csrf_token(client, user, tenant):
    csrf = dashboard_login(client)
    body = {"name": "Dana", "email": "dana@example.com"}
    missing = client.post(f"{API}/approvers", json=body)
    assert missing.status_code == 403 and missing.json()["error"]["code"] == "csrf_failed"
    wrong = client.post(f"{API}/approvers", json=body, headers={"X-CSRF-Token": "0" * 64})
    assert wrong.status_code == 403
    ok = client.post(f"{API}/approvers", json=body, headers={"X-CSRF-Token": csrf})
    assert ok.status_code == 201


def test_dashboard_actions_are_attributed_to_the_users_backing_key(client, user, tenant, session):
    csrf = dashboard_login(client)
    a = create_approver(client, tenant, "Erin")["id"]
    response = client.post(
        f"{API}/policies",
        json={"name": "Deploys", "required_approvals": 1, "approver_ids": [a]},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201
    events = client.get(f"{API}/policies/{response.json()['id']}/audit-events").json()["items"]
    assert events[0]["event_data"]["actor_api_key_id"] == str(user["key_id"])


def test_logout_revokes_the_session(client, user, pending):
    csrf = dashboard_login(client)
    token = client.cookies.get(dashboard_auth_service.SESSION_COOKIE)
    assert client.post(f"{API}/auth/logout").status_code == 403  # CSRF required
    assert client.post(f"{API}/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    # Replaying the old cookie after logout does not work.
    client.cookies.set(dashboard_auth_service.SESSION_COOKIE, token, path="/api/v1")
    assert client.get(f"{API}/requests/{pending['id']}").status_code == 401


def test_expired_session_is_refused(client, user, pending, session_factory):
    dashboard_login(client)
    with session_factory() as db, transaction(db):
        db.execute(
            update(DashboardSession).values(
                created_at=utcnow() - timedelta(hours=10), expires_at=utcnow() - timedelta(hours=1)
            )
        )
    response = client.get(f"{API}/requests/{pending['id']}")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "session_invalid"


def test_disabling_the_user_or_revoking_its_key_ends_sessions(
    client, user, pending, session_factory
):
    dashboard_login(client)
    with session_factory() as db, transaction(db):
        db.execute(update(ApiKey).where(ApiKey.id == user["key_id"]).values(revoked_at=utcnow()))
    assert client.get(f"{API}/requests/{pending['id']}").status_code == 401


def test_disable_user_revokes_everything(client, user, pending, session_factory, session):
    dashboard_login(client)
    with session_factory() as db, transaction(db):
        dashboard_auth_service.disable_user(db, "OPS@example.com")
    assert client.get(f"{API}/requests/{pending['id']}").status_code == 401
    assert login(client).status_code == 401
    assert session.scalar(select(DashboardSession.revoked_at)) is not None


def test_repeated_failures_lock_the_account(client, user, test_settings, session_factory):
    for _ in range(test_settings.dashboard_max_failed_logins):
        assert login(client, password="wrong password entirely").status_code == 401
    # Locked: even the right password fails, with the same generic answer.
    locked = login(client)
    assert locked.status_code == 401 and locked.json()["error"]["code"] == "login_failed"
    with session_factory() as db, transaction(db):
        db.execute(update(DashboardUser).values(locked_until=utcnow() - timedelta(seconds=1)))
    assert login(client).status_code == 200


def test_successful_login_resets_the_failure_counter(client, user, session):
    login(client, password="wrong password entirely")
    assert login(client).status_code == 200
    session.expire_all()
    assert session.scalar(select(DashboardUser.failed_login_count)) == 0


def test_sessions_are_organization_scoped(client, user, make_tenant):
    other = make_tenant("Globex")
    a = create_approver(client, other, "Zed")["id"]
    policy = make_policy(client, other, [a])
    foreign = create_request(client, other, policy["id"])
    dashboard_login(client)
    assert client.get(f"{API}/requests/{foreign['id']}").status_code == 404
    assert client.get(f"{API}/policies/{policy['id']}").status_code == 404
    listed = client.get(f"{API}/requests").json()
    assert listed["total"] == 0


def test_api_keys_take_precedence_over_a_session_cookie(client, user, tenant, pending):
    dashboard_login(client)
    # An agent key without policies:read is still an agent key even if a cookie is present.
    assert client.get(f"{API}/policies", headers=tenant.agent).status_code == 403
    assert client.get(f"{API}/requests/{pending['id']}", headers=tenant.agent).status_code == 200


def test_votes_write_can_never_be_granted_to_dashboard_users(session_factory, tenant):
    with pytest.raises(ValueError, match="votes:write"):
        create_dashboard_user(
            session_factory,
            tenant.organization_id,
            email="voter@example.com",
            scopes=["requests:read", "votes:write"],
        )


def test_dashboard_session_cannot_vote_even_by_trying(client, user, pending, tenant):
    csrf = dashboard_login(client)
    approver = pending["authorized_approver_ids"][0]
    response = client.post(
        f"{API}/requests/{pending['id']}/votes",
        json={"approver_id": approver, "vote": "approve"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 403


def test_weak_passwords_and_duplicate_emails_are_refused(session_factory, tenant, user):
    with pytest.raises(ValueError, match="password"):
        create_dashboard_user(
            session_factory, tenant.organization_id, email="x@example.com", password="short"
        )
    with pytest.raises(ValueError, match="already exists"):
        create_dashboard_user(session_factory, tenant.organization_id, email="Ops@Example.com")


def test_backing_key_is_never_usable_as_an_api_key(client, user, session):
    key = session.get(ApiKey, user["key_id"])
    assert key.name.startswith("dashboard:")
    # The plaintext was never produced, so nothing can present it; the hash is not a key.
    assert client.get(f"{API}/requests", headers={"X-API-Key": key.key_hash}).status_code == 401
    assert UUID(str(key.organization_id))
