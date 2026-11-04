"""Deterministic stand-in for Telegram, for tests and explicitly enabled local development.

Simulated taps and messages are stored in Telegram's own update shape and read back
through the real Telegram parser, so tests exercise the same parsing as production. It
is refused when APP_ENV=production (see Settings). With TEXT_MOCK_STATE_DIR set, the
text worker and the text-simulate CLI share state through files.
"""

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.text.base import Button, ErrorKind, OutgoingMessage, TextProviderError, UpdateBatch
from app.text.telegram import parse_updates

KINDS = ("messages", "inbox", "outbox")


class _MemoryStore:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {kind: {} for kind in KINDS}
        self._lock = threading.Lock()

    def put(self, kind: str, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._data[kind][key] = json.loads(json.dumps(value))

    def get(self, kind: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._data[kind].get(key)
            return json.loads(json.dumps(value)) if value is not None else None

    def keys(self, kind: str) -> list[str]:
        with self._lock:
            return sorted(self._data[kind])

    def delete(self, kind: str, key: str) -> None:
        with self._lock:
            self._data[kind].pop(key, None)


class _FileStore:
    def __init__(self, root: str) -> None:
        self._root = Path(root)
        for kind in KINDS:
            (self._root / kind).mkdir(parents=True, exist_ok=True)

    def _path(self, kind: str, key: str) -> Path:
        if not key.replace("-", "").isalnum():
            raise ValueError("invalid mock key")
        return self._root / kind / f"{key}.json"

    def put(self, kind: str, key: str, value: dict[str, Any]) -> None:
        directory = self._root / kind
        fd, temp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        os.replace(temp, self._path(kind, key))

    def get(self, kind: str, key: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._path(kind, key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def keys(self, kind: str) -> list[str]:
        return sorted(p.stem for p in (self._root / kind).glob("*.json"))

    def delete(self, kind: str, key: str) -> None:
        self._path(kind, key).unlink(missing_ok=True)


class MockTextProvider:
    name = "mock"

    def __init__(self, settings: Settings) -> None:
        directory = settings.text_mock_state_dir
        self._store = _FileStore(directory) if directory else _MemoryStore()
        self._last_id = 0
        # Test knob: errors raised by the next send_message calls, in order.
        self.fail_next: list[TextProviderError] = []
        self.answers: list[tuple[str, str]] = []

    # --- TextProvider -------------------------------------------------------------------

    def _next_id(self) -> int:
        # Increasing (update offsets rely on it) and, in practice, unique across the
        # processes sharing a file store: microseconds since the epoch.
        self._last_id = max(self._last_id + 1, time.time_ns() // 1000)
        return self._last_id

    def send_message(self, message: OutgoingMessage) -> str:
        if self.fail_next:
            raise self.fail_next.pop(0)
        message_id = str(self._next_id())
        self._store.put(
            "messages",
            message_id,
            {
                "chat_id": message.chat_id,
                "text": message.text,
                "buttons": [{"label": b.label, "data": b.data} for b in message.buttons],
                "edits": 0,
            },
        )
        self._outbox("message", chat_id=message.chat_id, text=message.text)
        return message_id

    def edit_message(
        self, chat_id: str, message_id: str, text: str, buttons: tuple[Button, ...]
    ) -> None:
        stored = self._store.get("messages", message_id)
        if stored is None or stored["chat_id"] != chat_id:
            raise TextProviderError(ErrorKind.PERMANENT, "http_400")
        stored.update(
            text=text,
            buttons=[{"label": b.label, "data": b.data} for b in buttons],
            edits=stored["edits"] + 1,
        )
        self._store.put("messages", message_id, stored)
        self._outbox("edit", chat_id=chat_id, text=text)

    def answer_callback(self, callback_id: str, text: str) -> None:
        self.answers.append((callback_id, text))
        self._outbox("answer", callback_id=callback_id, text=text)

    def get_updates(self, offset: int | None, timeout: int) -> UpdateBatch:
        raw: list[dict[str, Any]] = []
        for key in self._store.keys("inbox"):
            update = self._store.get("inbox", key)
            if update is None:
                continue
            if offset is not None and update["update_id"] < offset:
                self._store.delete("inbox", key)  # confirmed, as Telegram forgets them
            else:
                raw.append(update)
        raw.sort(key=lambda u: u["update_id"])
        return parse_updates(raw)

    def close(self) -> None:
        return None

    # --- Simulation helpers (tests and the text-simulate CLI) -----------------------------

    def _outbox(self, kind: str, **fields: str) -> None:
        self._store.put("outbox", f"{self._next_id():020d}", {"kind": kind, **fields})

    def outbox(self) -> list[dict[str, Any]]:
        return [v for k in self._store.keys("outbox") if (v := self._store.get("outbox", k))]

    def message(self, message_id: str) -> dict[str, Any] | None:
        return self._store.get("messages", message_id)

    def _enqueue(self, update: dict[str, Any]) -> int:
        update_id = self._next_id()
        self._store.put("inbox", f"{update_id:020d}", {"update_id": update_id, **update})
        return update_id

    def button_data(self, message_id: str, action: str) -> str:
        """The callback data of the button for ``action`` (a/r/c/x) on a sent message."""
        stored = self.message(message_id)
        for button in (stored or {}).get("buttons", []):
            if button["data"].split(":")[1:2] == [action]:
                return button["data"]
        raise LookupError("that button is not on the message now")

    def tap(
        self,
        data: str,
        *,
        sender_id: str,
        chat_id: str | None,
        message_id: str | None,
    ) -> int:
        message: dict[str, Any] = {}
        if message_id is not None:
            message["message_id"] = int(message_id)
        if chat_id is not None:
            message["chat"] = {"id": int(chat_id), "type": "private"}
        return self._enqueue(
            {
                "callback_query": {
                    "id": f"cb{self._next_id()}",
                    "from": {"id": int(sender_id), "is_bot": False},
                    "message": message,
                    "data": data,
                }
            }
        )

    def tap_button(self, message_id: str, action: str, *, sender_id: str) -> int:
        stored = self.message(message_id) or {}
        return self.tap(
            self.button_data(message_id, action),
            sender_id=sender_id,
            chat_id=stored.get("chat_id"),
            message_id=message_id,
        )

    def send_text(
        self, text: str, *, sender_id: str, chat_id: str | None = None, chat_type: str = "private"
    ) -> int:
        return self._enqueue(
            {
                "message": {
                    "message_id": 1,
                    "from": {"id": int(sender_id), "is_bot": False},
                    "chat": {"id": int(chat_id or sender_id), "type": chat_type},
                    "text": text,
                }
            }
        )
