from __future__ import annotations

from waggle.markdown_vault import slugify, vault_filename
from waggle.models import Node, NodeType


def _node(label: str, node_id: str = "abc-123") -> Node:
    return Node(label=label, content="x", node_type=NodeType.NOTE, id=node_id)


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


def test_slugify_ascii_passthrough():
    assert slugify("Hello World") == "hello-world"


def test_slugify_multiple_spaces_collapse():
    assert slugify("a   b") == "a-b"


def test_slugify_multiple_dashes_collapse():
    assert slugify("a---b") == "a-b"


def test_slugify_leading_trailing_separators_stripped():
    assert slugify("---abc---") == "abc"


def test_slugify_unicode_strips_non_ascii():
    # é is not in [a-z0-9], so it becomes a separator that gets stripped
    assert slugify("café") == "caf"


def test_slugify_empty_returns_node():
    assert slugify("") == "node"


def test_slugify_symbols_only_returns_node():
    assert slugify("!!!") == "node"


def test_slugify_dashes_only_returns_node():
    assert slugify("---") == "node"


def test_slugify_lowercase_normalization():
    assert slugify("HELLO") == "hello"


def test_slugify_numbers_preserved():
    assert slugify("issue 42") == "issue-42"


# ---------------------------------------------------------------------------
# vault_filename
# ---------------------------------------------------------------------------


def test_vault_filename_normal():
    node = _node(label="My Note", node_id="abc-123")
    assert vault_filename(node) == "my-note--abc-123.md"


def test_vault_filename_empty_label_fallback():
    # Node validator rejects empty labels, so use a label that slugifies to "node"
    node = _node(label="!!!", node_id="xyz-999")
    assert vault_filename(node) == "node--xyz-999.md"
