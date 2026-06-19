from __future__ import annotations

from waggle.markdown_vault import parse_frontmatter, render_frontmatter


class TestFrontmatter:
    def test_empty_payload_round_trip(self):
        rendered = render_frontmatter({})
        payload, _ = parse_frontmatter(rendered)
        assert payload == {}

    def test_string_value_round_trip(self):
        original = {"key": "hello world"}
        rendered = render_frontmatter(original)
        payload, _ = parse_frontmatter(rendered)
        assert payload == original

    def test_integer_value_preserves_type(self):
        original = {"count": 42}
        rendered = render_frontmatter(original)
        payload, _ = parse_frontmatter(rendered)
        assert payload["count"] == 42
        assert isinstance(payload["count"], int)

    def test_list_value_round_trip(self):
        original = {"tags": ["a", "b", "c"]}
        rendered = render_frontmatter(original)
        payload, _ = parse_frontmatter(rendered)
        assert payload == original

    def test_special_characters_round_trip(self):
        original = {
            "url": "https://example.com/path?q=1&r=2",
            "quoted": 'say "hello"',
            "backslash": "C:\\Users\\test",
        }
        rendered = render_frontmatter(original)
        payload, _ = parse_frontmatter(rendered)
        assert payload == original

    def test_no_frontmatter_returns_empty_dict(self):
        text = "# Just a heading\n\nSome content."
        payload, body = parse_frontmatter(text)
        assert payload == {}
        assert body == text

    def test_single_delimiter_does_not_crash(self):
        text = "---\nkey: 1\nno closing delimiter"
        payload, body = parse_frontmatter(text)
        assert isinstance(payload, dict)
        assert isinstance(body, str)
