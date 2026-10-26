"""Password hashing for dashboard users.

Uses scrypt from the Python standard library (OpenSSL-backed ``hashlib.scrypt``), a
memory-hard key-derivation function designed for password storage. The stored format is
self-describing so parameters can be raised later without breaking existing hashes:

    scrypt$<log2 n>$<r>$<p>$<salt b64>$<hash b64>
"""

import base64
import hashlib
import hmac
import secrets

_ALGORITHM = "scrypt"
_LOG2_N = 15  # n = 32768
_R = 8
_P = 1
_SALT_BYTES = 16
_HASH_BYTES = 32
# 128 * r * n bytes are needed (32 MiB for the defaults); allow headroom.
_MAXMEM = 128 * 1024 * 1024

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256


def _derive(password: str, salt: bytes, log2_n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**log2_n,
        r=r,
        p=p,
        maxmem=_MAXMEM,
        dklen=_HASH_BYTES,
    )


def hash_password(password: str) -> str:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(
            f"password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters long"
        )
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(password, salt, _LOG2_N, _R, _P)
    encode = base64.b64encode
    return f"{_ALGORITHM}${_LOG2_N}${_R}${_P}${encode(salt).decode()}${encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time comparison; any malformed stored value simply fails."""
    if len(password) > MAX_PASSWORD_LENGTH:
        return False
    try:
        algorithm, log2_n, r, p, salt, digest = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        expected = base64.b64decode(digest)
        actual = _derive(password, base64.b64decode(salt), int(log2_n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# A valid hash of a random password: verifying against it when the email is unknown
# keeps the response time similar to a real check (no user enumeration by timing).
DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(24))
