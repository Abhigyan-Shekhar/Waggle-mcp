from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from waggle.models import Node, NodeType, RelationType, normalize_relationship


def test_node_type_values_include_expected_defaults() -> None:
    assert {
        "fact",
        "entity",
        "concept",
        "preference",
        "decision",
        "question",
        "note",
    }.issubset({node_type.value for node_type in NodeType})
    assert NodeType("fact") is NodeType.FACT

    with pytest.raises(ValueError):
        NodeType("unknown")


def test_relation_type_values_include_expected_defaults() -> None:
    assert {
        "relates_to",
        "contradicts",
        "depends_on",
        "part_of",
        "updates",
        "derived_from",
        "similar_to",
    }.issubset({relation_type.value for relation_type in RelationType})
    assert RelationType("relates_to") is RelationType.RELATES_TO

    with pytest.raises(ValueError):
        RelationType("unknown")


def test_normalize_relationship_accepts_enum_and_normalizes_strings() -> None:
    assert normalize_relationship(RelationType.RELATES_TO) == "relates_to"
    assert normalize_relationship("  RELATES_TO  ") == "relates_to"

    with pytest.raises(ValueError, match="Relationship cannot be empty"):
        normalize_relationship("   ")


def test_node_defaults_for_minimal_node() -> None:
    before = datetime.now(UTC)
    node = Node(label="A useful fact", content="The model has stable defaults.", node_type=NodeType.FACT)
    after = datetime.now(UTC)

    UUID(node.id)
    assert node.created_at.tzinfo is UTC
    assert before <= node.created_at <= after
    assert before <= node.updated_at <= after
    assert node.access_count == 0
    assert node.tags == []
    assert node.aliases == []
    assert node.metadata == {}
    assert node.node_type is NodeType.FACT
