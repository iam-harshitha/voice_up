"""Telegram adapter: parsing, request shape and error mapping, with no network.

Every HTTP exchange goes through httpx.MockTransport. The token (a fixture value in the
BotFather format, not a real token) must never appear in an error, a log or a message.
"""

import json
import logging

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.logging import configure_logging
from app.text.base import (
    Button,
    CallbackTap,
    ErrorKind,
    IncomingText,
    OutgoingMessage,
    TextProviderError,
)
from app.text.telegram import TelegramProvider, parse_update, parse_updates

FAKE_TOKEN = "123456789:" + "F" * 35  # fixture value in BotFather's format


def settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://u:p@localhost/x_test",
        text_provider="telegram",
        telegram_bot_token=SecretStr(FAKE_TOKEN),
        telegram_bot_username="approval_line_test_bot",
        text_callback_secret=SecretStr("s" * 40),
    )


def provider(handler) -> TelegramProvider:
    return TelegramProvider(settings(), transport=httpx.MockTransport(handler))


def ok(result) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def test_parse_callback_and_message_updates():
    tap = parse_update(
        {
            "update_id": 10,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 7000001},
                "message": {"message_id": 55, "chat": {"id": 7000001, "type": "private"}},
                "data": "al1:a:xyz",
            },
        }
    )
    assert tap == CallbackTap(10, "cb1", "7000001", "7000001", "55", "al1:a:xyz")
    text = parse_update(
        {
            "update_id": 11,
            "message": {
                "from": {"id": 7000001},
                "chat": {"id": 7000001, "type": "private"},
                "text": "/start abc",
            },
        }
    )
    assert text == IncomingText(11, "7000001", "7000001", "private", "/start abc")


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {"update_id": "1"},
        {"update_id": 1, "callback_query": {"id": "x", "from": {"id": True}, "data": "d"}},
        {"update_id": 1, "message": {"chat": {"id": 1}}},  # no sender
        {"update_id": 1, "edited_message": {}},
    ],
)
def test_unusable_updates_are_dropped(raw):
    assert parse_update(raw) is None


def test_offset_advances_past_dropped_updates():
    batch = parse_updates([{"update_id": 5, "edited_message": {}}, {"update_id": 4}])
    assert batch.updates == [] and batch.next_offset == 6


def test_send_message_is_plain_text_with_inline_buttons():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return ok({"message_id": 42})

    message_id = provider(handler).send_message(
        OutgoingMessage("7000001", "Approve?", (Button("Approve", "al1:a:x"),))
    )
    assert message_id == "42"
    assert seen["path"].endswith("/sendMessage")
    assert "parse_mode" not in seen["body"]
    assert seen["body"]["reply_markup"] == {
        "inline_keyboard": [[{"text": "Approve", "callback_data": "al1:a:x"}]]
    }


@pytest.mark.parametrize(
    ("response", "kind", "code"),
    [
        (httpx.Response(429, json={"ok": False}), ErrorKind.RETRYABLE, "http_429"),
        (httpx.Response(502, text="bad gateway"), ErrorKind.RETRYABLE, "http_502"),
        (httpx.Response(401, json={"ok": False}), ErrorKind.PERMANENT, "unauthorized"),
        (httpx.Response(403, json={"ok": False}), ErrorKind.PERMANENT, "forbidden"),
        (
            httpx.Response(400, json={"ok": False, "description": "chat not found"}),
            ErrorKind.PERMANENT,
            "http_400",
        ),
        (ok({}), ErrorKind.RETRYABLE, "malformed_response"),
    ],
)
def test_errors_map_to_safe_codes(response, kind, code):
    with pytest.raises(TextProviderError) as caught:
        provider(lambda _: response).send_message(OutgoingMessage("1", "x"))
    assert (caught.value.kind, caught.value.code) == (kind, code)
    assert FAKE_TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    ("exc", "code"),
    [(httpx.ReadTimeout("timed out"), "timeout"), (httpx.ConnectError("refused"), "connect_error")],
)
def test_network_errors_are_retryable_and_hide_the_url(exc, code):
    def handler(request):
        raise exc

    with pytest.raises(TextProviderError) as caught:
        provider(handler).get_updates(None, 0)
    assert caught.value.kind is ErrorKind.RETRYABLE and caught.value.code == code
    assert caught.value.__cause__ is None  # the httpx exception (with the URL) is dropped
    assert FAKE_TOKEN not in str(caught.value)


def test_edit_to_the_same_content_is_not_an_error():
    response = httpx.Response(400, json={"ok": False, "description": "message is not modified"})
    provider(lambda _: response).edit_message("1", "2", "same", ())


def test_the_token_never_reaches_the_logs(caplog, capsys):
    configure_logging("DEBUG")
    caplog.set_level(logging.DEBUG)
    p = provider(lambda _: ok({"message_id": 1}))
    p.send_message(OutgoingMessage("7000001", "hello"))
    assert FAKE_TOKEN not in caplog.text
    assert FAKE_TOKEN not in capsys.readouterr().out
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
