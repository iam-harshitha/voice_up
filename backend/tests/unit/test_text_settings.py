"""Text settings: off by default, validated, and never echoing a secret."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings

DB = "postgresql+psycopg://u:p@localhost/x_test"
TOKEN = "123456789:" + "T" * 35  # fixture value in BotFather's format
SECRET = "c" * 40


def build(**values) -> Settings:
    return Settings(_env_file=None, database_url=DB, **values)


def test_text_is_disabled_by_default():
    assert build().text_provider == "disabled"


def test_empty_env_values_are_fine_while_disabled():
    s = build(telegram_bot_token="", text_callback_secret="", telegram_bot_username="")
    assert s.text_provider == "disabled" and s.telegram_bot_username is None


def test_mock_is_refused_in_production():
    with pytest.raises(ValidationError, match="TEXT_PROVIDER=mock is not allowed"):
        build(app_env="production", text_provider="mock", text_callback_secret=SECRET)


@pytest.mark.parametrize("secret", [None, "short"])
def test_callback_secret_must_be_32_characters(secret):
    with pytest.raises(ValidationError, match="TEXT_CALLBACK_SECRET"):
        build(text_provider="mock", text_callback_secret=secret)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"telegram_bot_username": "my_demo_bot"}, "requires TELEGRAM_BOT_TOKEN"),
        ({"telegram_bot_token": TOKEN}, "requires TELEGRAM_BOT_USERNAME"),
        (
            {"telegram_bot_token": "not-a-token-" + "q" * 30, "telegram_bot_username": "my_bot1"},
            "does not look like a BotFather token",
        ),
        (
            {
                "telegram_bot_token": TOKEN,
                "telegram_bot_username": "my_bot1",
                "telegram_api_base_url": "http://api.telegram.org",
            },
            "must use https",
        ),
    ],
)
def test_telegram_needs_a_token_and_a_username(values, message):
    with pytest.raises(ValidationError, match=message) as caught:
        build(text_provider="telegram", text_callback_secret=SECRET, **values)
    assert TOKEN not in str(caught.value)
    assert "q" * 30 not in str(caught.value)  # a malformed token is not echoed either


def test_a_valid_telegram_configuration_hides_its_secrets():
    s = build(
        text_provider="telegram",
        telegram_bot_token=TOKEN,
        telegram_bot_username="@Approval_Line_bot",
        text_callback_secret=SECRET,
    )
    assert s.telegram_bot_username == "Approval_Line_bot"
    assert TOKEN not in repr(s) and SECRET not in repr(s)


def test_long_poll_must_fit_inside_the_http_timeout():
    with pytest.raises(ValidationError, match="TEXT_HTTP_TIMEOUT_SECONDS"):
        build(
            text_provider="mock",
            text_callback_secret=SECRET,
            text_poll_timeout_seconds=10,
            text_http_timeout_seconds=5,
        )
