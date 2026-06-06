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


@pytest.mark.parametrize("relation_type", list(RelationType))
def test_normalize_relationship_accepts_canonical_relation_types(relation_type: RelationType) -> None:
    assert normalize_relationship(relation_type) == relation_type.value
    assert normalize_relationship(relation_type.value) == relation_type.value


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("  RELATES_TO  ", RelationType.RELATES_TO.value),
        ("Contradicts", RelationType.CONTRADICTS.value),
        ("related_to", "related_to"),
    ],
)
def test_normalize_relationship_handles_common_aliases_and_variations(raw_value: str, expected: str) -> None:
    assert normalize_relationship(raw_value) == expected


def test_normalize_relationship_preserves_unknown_non_empty_strings() -> None:
    assert normalize_relationship("  custom_edge  ") == "custom_edge"

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
