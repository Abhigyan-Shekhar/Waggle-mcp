"""Edge-case tests for waggle.auth.api_key_prefix and related helpers.

api_key_prefix extracts a short identifier from a raw API key. It is used in
error messages and audit logs, so its behavior on malformed or boundary inputs
should be locked down by tests.

Contract (see src/waggle/auth.py):
    - strip surrounding whitespace
    - if a "." is present, return everything before the first "."
    - otherwise return the first 16 characters
"""

from __future__ import annotations

import random
import string

from waggle.auth import api_key_prefix, generate_api_key, hash_api_key, verify_api_key


def test_api_key_prefix_splits_on_dot():
    assert api_key_prefix("sk_live_abcd.supersecretpart") == "sk_live_abcd"


def test_api_key_prefix_splits_on_first_dot_only():
    assert api_key_prefix("a.b.c") == "a"


def test_api_key_prefix_short_key_without_dot():
    assert api_key_prefix("short") == "short"


def test_api_key_prefix_long_key_without_dot_is_capped_at_16():
    key = "x" * 100
    result = api_key_prefix(key)
    assert result == "x" * 16
    assert len(result) == 16


def test_api_key_prefix_exactly_16_chars_without_dot():
    key = "a" * 16
    assert api_key_prefix(key) == key


def test_api_key_prefix_strips_surrounding_whitespace():
    assert api_key_prefix("  sk_live_abcd.secret  ") == "sk_live_abcd"


def test_api_key_prefix_empty_string():
    assert api_key_prefix("") == ""


def test_api_key_prefix_only_whitespace():
    assert api_key_prefix("    ") == ""


def test_api_key_prefix_only_a_dot():
    assert api_key_prefix(".") == ""


def test_api_key_prefix_no_dot_length_is_at_most_16_property():
    alphabet = string.ascii_letters + string.digits + "_-"
    rng = random.Random(0)  # seeded for determinism
    for _ in range(200):
        candidate = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 64)))
        assert len(api_key_prefix(candidate)) <= 16


def test_hash_api_key_is_deterministic():
    assert hash_api_key("sk_test_key.secret") == hash_api_key("sk_test_key.secret")


def test_verify_api_key_accepts_matching_key():
    key = generate_api_key("test")
    assert verify_api_key(key, hash_api_key(key)) is True


def test_verify_api_key_rejects_wrong_key():
    expected = hash_api_key(generate_api_key("test"))
    assert verify_api_key(generate_api_key("test"), expected) is False
