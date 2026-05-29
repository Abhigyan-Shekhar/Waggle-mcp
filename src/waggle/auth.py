from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from waggle.errors import AuthenticationError, AuthorizationError
from waggle.models import ApiKeyRecord


def hash_api_key(raw_api_key: str) -> str:
    digest = hashlib.sha256(raw_api_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def verify_api_key(raw_api_key: str, expected_hash: str) -> bool:
    candidate = hash_api_key(raw_api_key)
    return hmac.compare_digest(candidate, expected_hash)


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
    return datetime.utcnow().isoformat() + "Z"
