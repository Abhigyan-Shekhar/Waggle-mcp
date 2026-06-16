from datetime import UTC, datetime, timedelta

import pytest

from waggle.auth import (
    AuthenticatedPrincipal,
    api_key_prefix,
    generate_api_key,
    hash_api_key,
    normalize_api_key_environment,
    principal_from_record,
    verify_api_key,
)
from waggle.errors import AuthenticationError, AuthorizationError
from waggle.models import ApiKeyRecord


def test_api_key_prefix_standard_format():
    assert api_key_prefix("sk_live_abc123.secret_part_here") == "sk_live_abc123"


def test_api_key_prefix_multiple_dots_only_splits_on_first():
    assert api_key_prefix("sk_test_a.b.c") == "sk_test_a"


def test_api_key_prefix_short_key_without_dot():
    assert api_key_prefix("short") == "short"


def test_api_key_prefix_long_key_without_dot():
    assert api_key_prefix("a" * 100) == "a" * 16


def test_api_key_prefix_exactly_sixteen_characters():
    key = "a" * 16
    assert api_key_prefix(key) == key


def test_api_key_prefix_strips_whitespace():
    assert api_key_prefix("   sk_live_abc.secret   ") == "sk_live_abc"


def test_api_key_prefix_empty_string():
    assert api_key_prefix("") == ""


def test_api_key_prefix_whitespace_only():
    assert api_key_prefix("   ") == ""


def test_api_key_prefix_dot_only():
    assert api_key_prefix(".") == ""


def test_api_key_prefix_never_exceeds_sixteen_characters_without_dot():
    samples = [
        "",
        "a",
        "short",
        "a" * 16,
        "a" * 32,
        "a" * 100,
    ]

    for sample in samples:
        assert len(api_key_prefix(sample)) <= 16


def make_record():
    raw_key = "secret-key"

    record = ApiKeyRecord(
        api_key_id="key-1",
        tenant_id="tenant-1",
        key_hash=hash_api_key(raw_key),
        scopes=["graph:read"],
    )

    return raw_key, record


def test_hash_api_key_same_input_same_hash():
    assert hash_api_key("abc") == hash_api_key("abc")


def test_hash_api_key_different_inputs_different_hashes():
    assert hash_api_key("abc") != hash_api_key("xyz")


def test_verify_api_key_success():
    raw = "secret"
    hashed = hash_api_key(raw)

    assert verify_api_key(raw, hashed) is True


def test_verify_api_key_failure():
    hashed = hash_api_key("correct")

    assert verify_api_key("wrong", hashed) is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("live", "live"),
        ("LIVE", "live"),
        (" Test ", "test"),
        ("LOCAL", "local"),
    ],
)
def test_normalize_api_key_environment_valid(raw, expected):
    assert normalize_api_key_environment(raw) == expected


def test_normalize_api_key_environment_invalid():
    with pytest.raises(ValueError):
        normalize_api_key_environment("production")


def test_generate_api_key_test_prefix():
    key = generate_api_key("test")

    assert key.startswith("sk_test_")


def test_generate_api_key_live_prefix():
    key = generate_api_key("live")

    assert key.startswith("sk_live_")


def test_generate_api_key_contains_separator():
    key = generate_api_key()

    assert "." in key


def test_principal_from_record_none_record():
    with pytest.raises(AuthenticationError):
        principal_from_record(None, "secret")


def test_principal_from_record_inactive_record():
    raw_key, record = make_record()

    record.status = "inactive"

    with pytest.raises(AuthenticationError):
        principal_from_record(record, raw_key)


def test_principal_from_record_expired_record():
    raw_key, record = make_record()

    record.expires_at = datetime.now(UTC) - timedelta(days=1)

    with pytest.raises(AuthenticationError):
        principal_from_record(record, raw_key)


def test_principal_from_record_wrong_key():
    _, record = make_record()

    with pytest.raises(AuthenticationError):
        principal_from_record(record, "wrong-key")


def test_require_scope_success():
    principal = AuthenticatedPrincipal(
        api_key_id="1",
        tenant_id="tenant",
        scopes=("graph:read",),
    )
    principal.require_scope("graph:read")


def test_require_scope_failure():
    principal = AuthenticatedPrincipal(
        api_key_id="1",
        tenant_id="tenant",
        scopes=("graph:read",),
    )

    with pytest.raises(AuthorizationError):
        principal.require_scope("graph:write")
