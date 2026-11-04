"""Telegram Bot API adapter: the only module that knows Telegram's field names.

The bot token is part of every API URL (https://api.telegram.org/bot<token>/<method>),
so nothing here ever logs or raises a URL, an httpx exception's text or a raw response
body: failures become a TextProviderError with a short, fixed code.

Button taps and /start messages are received by long polling (getUpdates), so no public
URL or webhook is needed. Messages are sent as plain text (no parse_mode), so request
content can never inject formatting or links.
"""

import logging
from typing import Any

import httpx

from app.core.config import Settings
from app.text.base import (
    Button,
    CallbackTap,
    ErrorKind,
    IncomingText,
    OutgoingMessage,
    TextProviderError,
    Update,
    UpdateBatch,
)

MAX_TEXT = 4096
MAX_CALLBACK_ANSWER = 200
_ALLOWED_UPDATES = ["message", "callback_query"]

# Keep HTTP libraries quiet even where configure_logging was not called (the request
# line they log at INFO contains the token).
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


def _id(value: Any) -> str | None:
    # Telegram IDs are integers (chat IDs may be negative); bool is not an ID.
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def parse_update(raw: Any) -> Update | None:
    """Normalise one Telegram update. Anything unexpected is dropped (None)."""
    if not isinstance(raw, dict) or _id(raw.get("update_id")) is None:
        return None
    update_id = int(raw["update_id"])
    callback = raw.get("callback_query")
    if isinstance(callback, dict):
        sender = callback.get("from") if isinstance(callback.get("from"), dict) else {}
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        callback_id, data, sender_id = (
            callback.get("id"),
            callback.get("data"),
            _id(sender.get("id")),
        )
        if not isinstance(callback_id, str) or not isinstance(data, str) or sender_id is None:
            return None
        return CallbackTap(
            update_id=update_id,
            callback_id=callback_id[:128],
            sender_id=sender_id,
            chat_id=_id(chat.get("id")),
            message_id=_id(message.get("message_id")),
            data=data[:128],
        )
    message = raw.get("message")
    if isinstance(message, dict):
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        sender_id, chat_id = _id(sender.get("id")), _id(chat.get("id"))
        if sender_id is None or chat_id is None:
            return None
        text = message.get("text")
        chat_type = chat.get("type")
        return IncomingText(
            update_id=update_id,
            sender_id=sender_id,
            chat_id=chat_id,
            chat_type=chat_type if isinstance(chat_type, str) else "unknown",
            text=text[:256] if isinstance(text, str) else "",
        )
    return None


def parse_updates(payload: Any) -> UpdateBatch:
    if not isinstance(payload, list):
        raise TextProviderError(ErrorKind.RETRYABLE, "malformed_updates")
    updates: list[Update] = []
    highest: int | None = None
    for raw in payload:
        if isinstance(raw, dict) and _id(raw.get("update_id")) is not None:
            highest = max(highest or 0, int(raw["update_id"]))
        update = parse_update(raw)
        if update is not None:
            updates.append(update)
    return UpdateBatch(updates, highest + 1 if highest is not None else None)


def username_mismatch(configured: str | None, actual: str) -> str | None:
    """A fix message when TELEGRAM_BOT_USERNAME is not the token's bot, else None.

    Pairing links are t.me/<TELEGRAM_BOT_USERNAME>?start=<code>; with the wrong username
    they open some other Telegram account, and Start never reaches this bot.
    """
    if (configured or "").lower() == actual.lower():
        return None
    return (
        f"TELEGRAM_BOT_USERNAME is '{configured}' but the bot token belongs to @{actual}: "
        f"pairing links would open a different Telegram account. Set "
        f"TELEGRAM_BOT_USERNAME={actual} in backend/.env and restart the API and text worker."
    )


def keyboard(buttons: tuple[Button, ...]) -> dict[str, Any]:
    """One row of inline buttons (an empty keyboard removes the buttons)."""
    return {"inline_keyboard": [[{"text": b.label, "callback_data": b.data} for b in buttons]]}


class TelegramProvider:
    name = "telegram"

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        token = settings.telegram_bot_token
        if token is None or not token.get_secret_value():  # also enforced by Settings
            raise ValueError("TELEGRAM_BOT_TOKEN is not set")
        timeout = settings.text_http_timeout_seconds
        self._client = httpx.Client(
            base_url=f"{settings.telegram_api_base_url.rstrip('/')}/bot{token.get_secret_value()}",
            timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            follow_redirects=False,
            transport=transport,
        )

    def _call(self, method: str, payload: dict[str, Any], *, read_timeout: float | None = None):
        try:
            kwargs: dict[str, Any] = {"json": payload}
            if read_timeout is not None:
                kwargs["timeout"] = httpx.Timeout(read_timeout, connect=5.0)
            response = self._client.post(f"/{method}", **kwargs)
        except httpx.TimeoutException:
            raise TextProviderError(ErrorKind.RETRYABLE, "timeout") from None
        except httpx.HTTPError:
            raise TextProviderError(ErrorKind.RETRYABLE, "connect_error") from None
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = None
        if status == 200 and isinstance(body, dict) and body.get("ok") is True:
            return body.get("result")
        description = body.get("description", "") if isinstance(body, dict) else ""
        if status == 400 and isinstance(description, str) and "not modified" in description:
            return None  # editing a message to its current content is harmless
        if status == 429 or status >= 500:
            raise TextProviderError(ErrorKind.RETRYABLE, f"http_{status}")
        if status in (401, 404):
            raise TextProviderError(ErrorKind.PERMANENT, "unauthorized")  # bad or revoked token
        if status == 403:
            raise TextProviderError(ErrorKind.PERMANENT, "forbidden")  # e.g. bot blocked
        raise TextProviderError(ErrorKind.PERMANENT, f"http_{status}")

    def send_message(self, message: OutgoingMessage) -> str:
        payload: dict[str, Any] = {"chat_id": message.chat_id, "text": message.text[:MAX_TEXT]}
        if message.buttons:
            payload["reply_markup"] = keyboard(message.buttons)
        result = self._call("sendMessage", payload)
        message_id = _id(result.get("message_id")) if isinstance(result, dict) else None
        if message_id is None:
            raise TextProviderError(ErrorKind.RETRYABLE, "malformed_response")
        return message_id

    def edit_message(
        self, chat_id: str, message_id: str, text: str, buttons: tuple[Button, ...]
    ) -> None:
        self._call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": int(message_id),
                "text": text[:MAX_TEXT],
                "reply_markup": keyboard(buttons),
            },
        )

    def answer_callback(self, callback_id: str, text: str) -> None:
        self._call(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text[:MAX_CALLBACK_ANSWER]},
        )

    def get_me(self) -> str:
        """The bot's real @username, as Telegram knows it for this token."""
        result = self._call("getMe", {})
        username = result.get("username") if isinstance(result, dict) else None
        if not isinstance(username, str) or not username:
            raise TextProviderError(ErrorKind.RETRYABLE, "malformed_response")
        return username

    def get_updates(self, offset: int | None, timeout: int) -> UpdateBatch:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": _ALLOWED_UPDATES}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, read_timeout=timeout + 10.0)
        return parse_updates(result)

    def close(self) -> None:
        self._client.close()
