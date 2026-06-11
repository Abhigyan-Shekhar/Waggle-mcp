from waggle.auth import api_key_prefix


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
