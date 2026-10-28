"""Dashboard security primitives that need no database."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.passwords import hash_password, verify_password
from app.services import dashboard_auth_service as auth

DB = "postgresql+psycopg://user:pass@localhost:5432/approval_line"


def settings(**values) -> Settings:
    return Settings(_env_file=None, database_url=DB, **values)


def test_password_hashes_are_salted_and_verifiable():
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")
    assert first != second and first.startswith("scrypt$")
    assert verify_password("correct horse battery staple", first)
    assert not verify_password("correct horse battery stapler", first)


MALFORMED = ["", "plain", "md5$x$y", "scrypt$15$8$1$!!$!!", "scrypt$a$b$c$d$e"]


@pytest.mark.parametrize("stored", MALFORMED)
def test_malformed_hashes_never_verify(stored):
    assert verify_password("anything at all", stored) is False


@pytest.mark.parametrize("password", ["short", "x" * 257])
def test_password_length_is_bounded(password):
    with pytest.raises(ValueError):
        hash_password(password)


def test_csrf_token_is_bound_to_the_session_token():
    token = "a" * 43
    csrf = auth.csrf_token_for(token)
    assert csrf != token and auth.csrf_matches(token, csrf)
    assert not auth.csrf_matches("b" * 43, csrf)
    assert not auth.csrf_matches(token, None)
    assert not auth.csrf_matches(token, "")
    assert not auth.csrf_matches(token, "x" * 500)


def test_session_token_hash_is_not_the_token():
    assert auth.hash_token("abc") != "abc" and len(auth.hash_token("abc")) == 64


def test_cookie_secure_defaults_by_environment():
    assert settings(app_env="development").session_cookie_secure is False
    assert settings(app_env="production").session_cookie_secure is True
    empty = settings(app_env="development", dashboard_cookie_secure="")
    assert empty.session_cookie_secure is False
    forced = settings(app_env="development", dashboard_cookie_secure=True)
    assert forced.session_cookie_secure is True


def test_insecure_cookies_are_refused_in_production():
    with pytest.raises(ValidationError, match="DASHBOARD_COOKIE_SECURE"):
        settings(app_env="production", dashboard_cookie_secure=False)


def test_votes_write_is_forbidden_for_dashboard_users():
    assert "votes:write" in auth.FORBIDDEN_DASHBOARD_SCOPES


# --- Production fails closed (Chunk 5) ----------------------------------------------------


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"voice_provider": "mock"}, "VOICE_PROVIDER=mock"),
        ({"dashboard_cookie_secure": False}, "DASHBOARD_COOKIE_SECURE"),
        ({"cors_origins": "*"}, "wildcard"),
        ({"cors_origins": "http://dashboard.example.com"}, "https"),
        ({"voice_provider": "bolna"}, "BOLNA_API_KEY"),
        (
            {
                "voice_provider": "bolna",
                "bolna_api_key": "k" * 20,
                "bolna_agent_id": "agent",
                "voice_webhook_token": "short",
            },
            "VOICE_WEBHOOK_TOKEN",
        ),
    ],
)
def test_unsafe_production_configuration_is_refused(values, message):
    with pytest.raises(ValidationError, match=message):
        settings(app_env="production", **values)


def test_safe_production_configuration_is_accepted():
    prod = settings(
        app_env="production",
        cors_origins="https://dashboard.example.com",
        voice_provider="bolna",
        bolna_api_key="k" * 20,
        bolna_agent_id="agent",
        voice_webhook_token="t" * 40,
    )
    assert prod.session_cookie_secure is True and prod.voice_provider == "bolna"
    assert "t" * 40 not in repr(prod) and "k" * 20 not in repr(prod)


def test_production_app_hides_docs_and_debug():
    from app.main import create_app

    app = create_app(settings(app_env="production"))
    assert app.docs_url is None and app.openapi_url is None and app.debug is False


def test_development_http_origins_remain_allowed():
    assert settings(cors_origins="http://localhost:5173").cors_origins == ["http://localhost:5173"]
