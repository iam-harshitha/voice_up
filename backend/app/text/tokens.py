"""Signed, single-use button tokens and one-time pairing codes.

Callback data (at most 64 bytes, Telegram's limit):

    al1:<action>:<nonce>:<signature>

- action: a = approve, r = reject, c = confirm approve, x = cancel
- nonce: 16 random bytes (22 base64url characters); only its SHA-256 is stored
- signature: HMAC-SHA256 under TEXT_CALLBACK_SECRET over "al1|<action>|<nonce>",
  truncated to 22 base64url characters (128+ bits)

The signature binds the action to the nonce, so swapping Approve for Reject breaks it.
Single use is enforced by the database: the stored hash must belong to a prompt in the
matching status, which a successful tap changes under a row lock.
"""

import base64
import hashlib
import hmac
import re
import secrets

PREFIX = "al1"
APPROVE, REJECT, CONFIRM, CANCEL = "a", "r", "c", "x"
ACTIONS = (APPROVE, REJECT, CONFIRM, CANCEL)
FIRST_STAGE = (APPROVE, REJECT)  # valid while the prompt is "sent"
CONFIRM_STAGE = (CONFIRM, CANCEL)  # valid while the prompt is "confirming"

_NONCE = re.compile(r"[A-Za-z0-9_-]{22}")
_SIGNATURE_CHARS = 22
_PAIRING_CODE = re.compile(r"[A-Za-z0-9_-]{32}")


def new_nonce() -> str:
    return secrets.token_urlsafe(16)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _signature(secret: str, action: str, nonce: str) -> str:
    mac = hmac.new(
        secret.encode("utf-8"), f"{PREFIX}|{action}|{nonce}".encode(), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")[:_SIGNATURE_CHARS]


def sign(secret: str, action: str, nonce: str) -> str:
    if action not in ACTIONS or not _NONCE.fullmatch(nonce):
        raise ValueError("invalid action or nonce")
    return f"{PREFIX}:{action}:{nonce}:{_signature(secret, action, nonce)}"


def parse(data: str) -> tuple[str, str, str] | None:
    """Split callback data into (action, nonce, signature) without verifying it."""
    if not isinstance(data, str) or len(data) > 64:
        return None
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != PREFIX:
        return None
    _, action, nonce, signature = parts
    if action not in ACTIONS or not _NONCE.fullmatch(nonce):
        return None
    return action, nonce, signature


def verify(secret: str, data: str) -> tuple[str, str] | None:
    """Return (action, nonce) if the signature is valid, else None (constant time)."""
    parsed = parse(data)
    if parsed is None:
        return None
    action, nonce, signature = parsed
    if not hmac.compare_digest(signature, _signature(secret, action, nonce)):
        return None
    return action, nonce


def new_pairing_code() -> str:
    """32 base64url characters (192 bits): valid as a Telegram /start parameter."""
    return secrets.token_urlsafe(24)


def is_pairing_code(value: str) -> bool:
    return bool(_PAIRING_CODE.fullmatch(value))
