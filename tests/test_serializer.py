from __future__ import annotations

from datetime import UTC, datetime

from waggle.models import (
    ConflictRecord,
    ContextTimelineItem,
    GraphStats,
    Node,
    NodeType,
    ObservationResult,
    TimelineResult,
)
from waggle.serializer import (
    serialize_observation_result,
    serialize_recent_nodes,
    serialize_stats,
    serialize_timeline,
)


# 1. serialize_stats
def test_serialize_stats_counts():
    stats = GraphStats(total_nodes=5, total_edges=3)
    output = serialize_stats(stats)
    assert "Total nodes: 5" in output
    assert "Total edges: 3" in output


def test_serialize_stats_empty():
    stats = GraphStats()
    output = serialize_stats(stats)
    assert "No nodes stored yet." in output


# 2. serialize_recent_nodes
def test_serialize_recent_nodes_empty():
    output = serialize_recent_nodes([])
    assert "No nodes stored" in output


def test_serialize_recent_nodes_nonempty():
    node = Node(
        label="My Decision",
        content="we chose PostgreSQL",
        node_type=NodeType.DECISION,
    )
    output = serialize_recent_nodes([node])
    assert "My Decision" in output


# 3. serialize_observation_result
def test_serialize_observation_result():
    node = Node(
        label="Use PostgreSQL",
        content="db choice",
        node_type=NodeType.DECISION,
    )
    conflict = ConflictRecord(
        other_node_id="abc123",
        other_node_label="Use MySQL",
        reason="contradicts earlier database choice",
    )
    result = ObservationResult(stored_nodes=[node], conflicts=[conflict])
    output = serialize_observation_result(result)
    assert "[STORED]" in output
    assert "Use PostgreSQL" in output
    assert "[CONFLICTS]" in output
    assert "Use MySQL" in output


# 4. serialize_timeline
def test_serialize_timeline_empty():
    result = TimelineResult(items=[])
    output = serialize_timeline(result)
    assert "No timeline items." in output


def test_serialize_timeline_nonempty():
    item = ContextTimelineItem(
        kind="node_created",
        timestamp=datetime(2024, 6, 15, tzinfo=UTC),
        label="Decision stored",
        summary="A node was saved",
    )
    result = TimelineResult(items=[item])
    output = serialize_timeline(result)
    assert "2024-06-15" in output
    assert "Decision stored" in output
