from __future__ import annotations

import pytest

from waggle.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictResolutionError,
    DanglingEdgeError,
    HashVerificationError,
    PayloadTooLargeError,
    RateLimitExceededError,
    SchemaVersionError,
    ServiceUnavailableError,
    ValidationFailure,
    WaggleError,
)

EXPECTED_ERROR_METADATA = {
    AuthenticationError: ("authentication_failed", 401),
    AuthorizationError: ("authorization_failed", 403),
    RateLimitExceededError: ("rate_limited", 429),
    PayloadTooLargeError: ("payload_too_large", 413),
    ServiceUnavailableError: ("service_unavailable", 503),
    ValidationFailure: ("validation_failed", 400),
    ConflictResolutionError: ("conflict_resolution_failed", 400),
    DanglingEdgeError: ("dangling_edge", 400),
    HashVerificationError: ("hash_verification_failed", 400),
    SchemaVersionError: ("schema_version_incompatible", 400),
}

assert set(WaggleError.__subclasses__()) == set(EXPECTED_ERROR_METADATA)


@pytest.mark.parametrize(
    ("error_cls", "expected_code", "expected_status_code"),
    [(cls, code, status) for cls, (code, status) in EXPECTED_ERROR_METADATA.items()],
)
def test_waggle_error_code_and_status_code(
    error_cls,
    expected_code,
    expected_status_code,
) -> None:
    error = error_cls("test message")
    assert error.code == expected_code
    assert error.status_code == expected_status_code
