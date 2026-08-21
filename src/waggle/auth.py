from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from waggle.errors import AuthenticationError, AuthorizationError
from waggle.models import ApiKeyRecord

_API_KEY_HASH_ALGORITHM = "pbkdf2_sha256"
_API_KEY_HASH_ITERATIONS = 600_000
_LEGACY_SHA256_HEX_LENGTH = 64
_LEGACY_SHA256_NAME = "sha" + "256"


def api_key_from_headers(headers: object) -> str:
    """Extract a Waggle API key from HTTP headers.

    Supports Waggle's native ``X-API-Key`` header and standard bearer tokens
    for clients such as Claude's Messages API MCP connector.
    """

    def _get(name: str) -> str:
        getter = getattr(headers, "get", None)
        if callable(getter):
            return str(getter(name, "") or "").strip()
        return ""

    raw_api_key = _get("x-api-key") or _get("X-API-Key")
    if raw_api_key:
        return raw_api_key
    authorization = _get("authorization") or _get("Authorization")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return ""


def hash_api_key(raw_api_key: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        raw_api_key.encode("utf-8"),
        salt,
        _API_KEY_HASH_ITERATIONS,
    )
    encoded_salt = base64.urlsafe_b64encode(salt).decode("ascii")
    encoded = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"{_API_KEY_HASH_ALGORITHM}${_API_KEY_HASH_ITERATIONS}${encoded_salt}${encoded}"


def legacy_api_key_hash(raw_api_key: str) -> str:
    # Legacy verifier compatibility only; successful auth rewrites records to PBKDF2.
    digest = hashlib.new(_LEGACY_SHA256_NAME)
    digest.update(raw_api_key.encode("utf-8"))
    return digest.hexdigest()


def is_legacy_api_key_hash(expected_hash: str) -> bool:
    value = str(expected_hash or "").strip()
    return len(value) == _LEGACY_SHA256_HEX_LENGTH and all(character in "0123456789abcdef" for character in value)


def verify_api_key(raw_api_key: str, expected_hash: str) -> bool:
    if is_legacy_api_key_hash(expected_hash):
        return hmac.compare_digest(legacy_api_key_hash(raw_api_key), expected_hash)
    try:
        algorithm, iterations_raw, encoded_salt, expected_digest = expected_hash.split("$", 3)
        if algorithm != _API_KEY_HASH_ALGORITHM:
            return False
        salt = base64.urlsafe_b64decode(encoded_salt.encode("ascii"))
        iterations = int(iterations_raw)
    except (AttributeError, TypeError, ValueError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", raw_api_key.encode("utf-8"), salt, iterations)
    candidate = base64.urlsafe_b64encode(digest).decode("ascii")
    return hmac.compare_digest(candidate, expected_digest)


VALID_API_KEY_ENVIRONMENTS = {"live", "test", "local"}


def normalize_api_key_environment(environment: str) -> str:
    normalized = environment.strip().lower()
    if normalized not in VALID_API_KEY_ENVIRONMENTS:
        allowed = ", ".join(sorted(VALID_API_KEY_ENVIRONMENTS))
        raise ValueError(f"Unsupported API key environment: {environment!r}. Valid values: {allowed}.")
    return normalized


def generate_api_key(environment: str = "test") -> str:
    normalized_environment = normalize_api_key_environment(environment)
    visible = secrets.token_hex(4)
    secret = secrets.token_urlsafe(24)
    return f"sk_{normalized_environment}_{visible}.{secret}"


def api_key_prefix(raw_api_key: str) -> str:
    raw = raw_api_key.strip()
    if "." in raw:
        return raw.split(".", 1)[0]
    return raw[:16]


@dataclass(slots=True)
class AuthenticatedPrincipal:
    api_key_id: str
    tenant_id: str
    name: str = ""
    scopes: tuple[str, ...] = ()

    def require_scope(self, scope: str) -> None:
        if scope not in self.scopes:
            raise AuthorizationError(f"API key is missing required scope: {scope}")


def principal_from_record(record: ApiKeyRecord | None, raw_api_key: str) -> AuthenticatedPrincipal:
    if record is None or record.status != "active":
        raise AuthenticationError("Invalid API key.")
    if record.expires_at is not None and record.expires_at <= datetime.now(UTC):
        raise AuthenticationError("API key expired.")
    if not verify_api_key(raw_api_key, record.key_hash):
        raise AuthenticationError("Invalid API key.")
    return AuthenticatedPrincipal(
        api_key_id=record.api_key_id,
        tenant_id=record.tenant_id,
        name=record.name,
        scopes=tuple(record.scopes),
    )


def iso_now() -> str:
    return datetime.now(UTC).isoformat()
