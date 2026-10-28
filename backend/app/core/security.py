"""API-key generation, hashing and the permission scopes they can carry."""

import hashlib
import secrets
from enum import StrEnum

API_KEY_PREFIX = "al_"
# 32 random bytes -> 256 bits of entropy, encoded as 43 URL-safe characters.
_API_KEY_RANDOM_BYTES = 32
MAX_API_KEY_LENGTH = 256


class Scope(StrEnum):
    POLICIES_READ = "policies:read"
    POLICIES_WRITE = "policies:write"
    APPROVERS_READ = "approvers:read"
    APPROVERS_WRITE = "approvers:write"
    REQUESTS_READ = "requests:read"
    REQUESTS_CREATE = "requests:create"
    VOTES_WRITE = "votes:write"
    VOICE_CALLS_CREATE = "voice:calls:create"
    VOICE_CALLS_READ = "voice:calls:read"
    AUDIT_READ = "audit:read"
    CONDITIONS_RESOLVE = "conditions:resolve"


ALL_SCOPES: frozenset[str] = frozenset(scope.value for scope in Scope)

# Minimum permissions for an AI agent: it may ask for approval and read the outcome,
# but it can never change policies or cast votes on its own requests.
AGENT_SCOPES: frozenset[str] = frozenset({Scope.REQUESTS_CREATE, Scope.REQUESTS_READ})


def generate_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(_API_KEY_RANDOM_BYTES)


def hash_api_key(plaintext: str) -> str:
    # The key is high-entropy random data, so a fast unsalted hash is sufficient:
    # brute-forcing 256 bits is infeasible regardless of hash speed.
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def parse_scopes(raw: str | list[str]) -> list[str]:
    """Normalise and validate a comma-separated string or list of scopes."""
    items = raw.split(",") if isinstance(raw, str) else raw
    scopes = sorted({item.strip() for item in items if item.strip()})
    if not scopes:
        raise ValueError("at least one scope is required")
    unknown = [scope for scope in scopes if scope not in ALL_SCOPES]
    if unknown:
        raise ValueError(
            f"unknown scope(s): {', '.join(unknown)}; valid scopes: {', '.join(sorted(ALL_SCOPES))}"
        )
    return scopes
