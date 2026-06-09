from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

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
    _format_updated_ago,
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


def test_format_updated_ago_under_one_minute():
    timestamp = datetime.now(UTC) - timedelta(seconds=30)
    assert _format_updated_ago(timestamp) == "just now"


def test_format_updated_ago_none_timestamp():
    assert _format_updated_ago(None) == "unknown"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (59, "just now"),
        (61, "1 minute ago"),
        (3599, "59 minutes ago"),
        (3601, "1 hour ago"),
        (86399, "23 hours ago"),
        (86401, "1 day ago"),
    ],
)
def test_format_updated_ago_around_bucket_boundaries(seconds, expected):
    timestamp = datetime.now(UTC) - timedelta(seconds=seconds)
    assert _format_updated_ago(timestamp) == expected


def test_format_updated_ago_at_one_minute_boundary():
    timestamp = datetime.now(UTC) - timedelta(seconds=60)
    assert _format_updated_ago(timestamp) == "1 minute ago"


def test_format_updated_ago_between_one_minute_and_one_hour():
    timestamp = datetime.now(UTC) - timedelta(seconds=125)
    assert _format_updated_ago(timestamp) == "2 minutes ago"


def test_format_updated_ago_at_one_hour_boundary():
    timestamp = datetime.now(UTC) - timedelta(seconds=3600)
    assert _format_updated_ago(timestamp) == "1 hour ago"


def test_format_updated_ago_between_one_hour_and_one_day():
    timestamp = datetime.now(UTC) - timedelta(seconds=7200)
    assert _format_updated_ago(timestamp) == "2 hours ago"


def test_format_updated_ago_at_one_day_boundary():
    timestamp = datetime.now(UTC) - timedelta(seconds=86400)
    assert _format_updated_ago(timestamp) == "1 day ago"


def test_format_updated_ago_over_one_day():
    timestamp = datetime.now(UTC) - timedelta(seconds=172800)
    assert _format_updated_ago(timestamp) == "2 days ago"


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
