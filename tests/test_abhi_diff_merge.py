"""
Unit and integration tests for the .abhi diff/merge tool.

Tests cover:
  16.1  Fixture-based diff, merge, and validation tests
  16.2  Schema version mismatch behaviour
  16.3  Boundary enforcement flags (dangling edges)
  16.4  resolve_abhi_conflict and upgrade_abhi_document
  16.5  CLI exit codes and --format=patch error
  16.6  serialize_abhi_diff truncation and MergeStrategyConfig.load()
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from waggle.abhi import (
    abhi_to_snapshot,
    build_abhi_document,
    diff_abhi_files,
    load_abhi_document,
    merge_abhi_documents,
    merge_abhi_files,
    resolve_abhi_conflict,
    serialize_abhi_diff,
    upgrade_abhi_document,
    validate_abhi_document,
    write_abhi_document,
)
from waggle.drive_sync import merge_downloaded_abhi
from waggle.errors import (
    ConflictResolutionError,
    DanglingEdgeError,
    SchemaVersionError,
    ValidationFailure,
)
from waggle.models import (
    FieldDelta,
    FieldLevelDiffResult,
    MergeStrategyConfig,
    NodeDiffRecord,
)

try:
    from waggle.server import app as cli_app

    _CLI_AVAILABLE = True
except ImportError:
    cli_app = None  # type: ignore[assignment]
    _CLI_AVAILABLE = False

_skip_cli = pytest.mark.skipif(
    not _CLI_AVAILABLE,
    reason="waggle.server requires mcp package",
)

try:
    from waggle.server import (
        _build_parser,
        _normalize_pull_strategy_args,
    )

    _PARSER_AVAILABLE = True
except ImportError:
    _build_parser = None  # type: ignore[assignment]
    _normalize_pull_strategy_args = None  # type: ignore[assignment]
    _PARSER_AVAILABLE = False

_skip_parser = pytest.mark.skipif(
    not _PARSER_AVAILABLE,
    reason="waggle.server requires MCP dependencies",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "abhi"

EMPTY = FIXTURES / "empty.abhi"
SINGLE_NODE = FIXTURES / "single-node.abhi"
LINEAR = FIXTURES / "linear-history.abhi"
BRANCHED = FIXTURES / "branched.abhi"
CONTRADICTIONS = FIXTURES / "with-contradictions.abhi"
DANGLING = FIXTURES / "with-dangling-edges.abhi"

ALL_VALID_FIXTURES = [EMPTY, SINGLE_NODE, LINEAR, BRANCHED, CONTRADICTIONS]


def _make_snapshot(nodes=None, edges=None):
    """Build a minimal snapshot dict."""
    return {
        "tenant_id": "test",
        "nodes": nodes or [],
        "edges": edges or [],
        "transcripts": [],
        "context_windows": [],
    }


def _node(label="Test", content="Test content", node_type="fact", **kwargs):
    from uuid import uuid4

    return {
        "id": str(uuid4()),
        "label": label,
        "content": content,
        "node_type": node_type,
        "tags": [],
        "aliases": [],
        "metadata": {},
        **kwargs,
    }


def _edge(source_id, target_id, relationship="relates_to", **kwargs):
    from uuid import uuid4

    return {
        "id": str(uuid4()),
        "source_id": source_id,
        "target_id": target_id,
        "relationship": relationship,
        "weight": 1.0,
        "metadata": {},
        **kwargs,
    }


# ---------------------------------------------------------------------------
# 16.1  Fixture-based tests
# ---------------------------------------------------------------------------


def test_drive_merge_respects_prefer_left_strategy(tmp_path):
    local_document = build_abhi_document(
        {
            "tenant_id": "test-tenant",
            "nodes": [
                {
                    "id": "shared-node",
                    "label": "Local label",
                    "content": "Local content",
                    "node_type": "note",
                    "tags": [],
                    "metadata": {},
                    "updated_at": "2026-06-10T10:00:00+00:00",
                }
            ],
            "edges": [],
            "transcripts": [],
            "context_windows": [],
        }
    )

    remote_document = build_abhi_document(
        {
            "tenant_id": "test-tenant",
            "nodes": [
                {
                    "id": "shared-node",
                    "label": "Remote label",
                    "content": "Remote content",
                    "node_type": "note",
                    "tags": [],
                    "metadata": {},
                    "updated_at": "2026-06-11T10:00:00+00:00",
                }
            ],
            "edges": [],
            "transcripts": [],
            "context_windows": [],
        }
    )

    output_path = tmp_path / "merged.abhi"

    merge_downloaded_abhi(
        local_document=local_document,
        remote_document=remote_document,
        output_path=output_path,
        merge_strategy="prefer_left",
    )

    merged_document = load_abhi_document(output_path)
    merged_nodes = [node for node in merged_document["nodes"] if node["id"] == "shared-node"]

    assert len(merged_nodes) == 1, f"Expected exactly one node with id 'shared-node', found {len(merged_nodes)}"

    merged_node = merged_nodes[0]

    assert merged_node["label"] == "Local label"
    assert merged_node["content"] == "Local content"


def test_drive_merge_rejects_invalid_strategy(tmp_path):
    document = build_abhi_document(_make_snapshot())

    with pytest.raises(ValidationFailure, match="Invalid merge_strategy"):
        merge_downloaded_abhi(
            local_document=document,
            remote_document=document,
            output_path=tmp_path / "merged.abhi",
            merge_strategy="invalid-strategy",
        )


def test_drive_merge_normalizes_strategy_whitespace(tmp_path):
    document = build_abhi_document(_make_snapshot())
    output_path = tmp_path / "normalized-strategy.abhi"

    merge_downloaded_abhi(
        local_document=document,
        remote_document=document,
        output_path=output_path,
        merge_strategy=" prefer_left ",
    )

    assert output_path.exists()


@pytest.mark.parametrize(
    "strategy_config",
    [
        MergeStrategyConfig(
            type_overrides=[
                {
                    "node_type": "fact",
                    "strategy": "invalid-strategy",
                }
            ]
        ),
        MergeStrategyConfig(
            field_overrides=[
                {
                    "field": "content",
                    "strategy": "invalid-strategy",
                }
            ]
        ),
    ],
)
def test_merge_rejects_invalid_override_strategies(
    tmp_path,
    strategy_config,
):
    document = build_abhi_document(_make_snapshot())

    with pytest.raises(ValidationFailure, match="Invalid merge_strategy"):
        merge_abhi_documents(
            document,
            document,
            document,
            base_input_path="local://base",
            left_input_path="local://left",
            right_input_path="local://right",
            output_path=tmp_path / "merged.abhi",
            merge_strategy="prefer_left",
            strategy_config=strategy_config,
            dry_run=True,
        )


def test_diff_empty_abhi():
    """Diff empty.abhi against itself — no changes."""
    result = diff_abhi_files(input_path_a=EMPTY, input_path_b=EMPTY)
    assert result.nodes_added == []
    assert result.nodes_removed == []
    assert result.nodes_updated == []
    assert result.edges_added == []
    assert result.edges_removed == []
    assert result.edges_updated == []


def test_diff_single_node_abhi():
    """Diff single-node.abhi against empty.abhi — 1 node added."""
    result = diff_abhi_files(input_path_a=EMPTY, input_path_b=SINGLE_NODE)
    assert len(result.nodes_added) == 1
    assert result.nodes_removed == []
    assert result.nodes_updated == []


def test_diff_linear_history():
    """Diff linear-history.abhi against itself — no changes."""
    result = diff_abhi_files(input_path_a=LINEAR, input_path_b=LINEAR)
    assert result.nodes_added == []
    assert result.nodes_removed == []
    assert result.nodes_updated == []
    assert result.edges_added == []
    assert result.edges_removed == []
    assert result.edges_updated == []


def test_merge_empty_abhi():
    """Merge empty.abhi as base/left/right — 0 nodes merged."""
    with tempfile.NamedTemporaryFile(suffix=".abhi", delete=False) as f:
        out_path = f.name
    try:
        result = merge_abhi_files(
            base_input_path=EMPTY,
            left_input_path=EMPTY,
            right_input_path=EMPTY,
            output_path=out_path,
        )
        assert result.nodes_merged == 0
        assert result.edges_merged == 0
        assert result.conflicts == []
    finally:
        Path(out_path).unlink(missing_ok=True)


def test_validate_all_fixtures():
    """Validate each valid fixture — assert valid=True."""
    for fixture in ALL_VALID_FIXTURES:
        doc = load_abhi_document(fixture)
        result = validate_abhi_document(doc, input_path=fixture)
        assert result.valid is True, f"{fixture.name} should be valid, errors: {result.errors}"


def test_validate_dangling_edges_fixture():
    """Validate with-dangling-edges.abhi — dangling_edge_count > 0."""
    doc = load_abhi_document(DANGLING)
    result = validate_abhi_document(doc, input_path=DANGLING)
    assert result.dangling_edge_count > 0
    # The validator reports dangling edges as warnings, not errors
    assert result.valid is True


# ---------------------------------------------------------------------------
# 16.2  Schema version mismatch tests
# ---------------------------------------------------------------------------


def test_schema_version_mismatch_minor():
    """Same major, different minor versions — schema_version_mismatch=True, no error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write two documents with the same content
        snap = _make_snapshot(nodes=[_node("N1", "Content 1")])
        path_a = Path(tmpdir) / "a.abhi"
        path_b = Path(tmpdir) / "b.abhi"
        write_abhi_document(snap, output_path=path_a)
        write_abhi_document(snap, output_path=path_b)

        # Load and patch the manifest schema_version to simulate minor mismatch
        doc_a = load_abhi_document(path_a)
        doc_b = load_abhi_document(path_b)
        doc_a["manifest"]["schema_version"] = "2.0.0"
        doc_b["manifest"]["schema_version"] = "2.1.0"

        from waggle.abhi import diff_abhi_documents

        result = diff_abhi_documents(
            doc_a,
            doc_b,
            input_path_a=path_a,
            input_path_b=path_b,
        )
        assert result.schema_version_mismatch is True
        assert len(result.warnings) > 0


def test_schema_version_mismatch_major():
    """Different major versions — SchemaVersionError raised."""
    with tempfile.TemporaryDirectory() as tmpdir:
        snap = _make_snapshot(nodes=[_node("N1", "Content 1")])
        path_a = Path(tmpdir) / "a.abhi"
        path_b = Path(tmpdir) / "b.abhi"
        write_abhi_document(snap, output_path=path_a)
        write_abhi_document(snap, output_path=path_b)

        doc_a = load_abhi_document(path_a)
        doc_b = load_abhi_document(path_b)
        # Patch to different major versions
        doc_a["manifest"]["schema_version"] = "1.0.0"
        doc_b["manifest"]["schema_version"] = "2.0.0"

        from waggle.abhi import diff_abhi_documents

        with pytest.raises(SchemaVersionError) as exc_info:
            diff_abhi_documents(
                doc_a,
                doc_b,
                input_path_a=path_a,
                input_path_b=path_b,
            )
        assert "waggle upgrade" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 16.3  Boundary enforcement flag tests
# ---------------------------------------------------------------------------


def test_import_dangling_edges_rejected():
    """abhi_to_snapshot on with-dangling-edges.abhi without flags — DanglingEdgeError."""
    doc = load_abhi_document(DANGLING)
    with pytest.raises(DanglingEdgeError):
        abhi_to_snapshot(doc, fallback_tenant_id="test")


def test_import_dangling_edges_allow_dangling():
    """abhi_to_snapshot with allow_dangling=True — no error, dangling edges dropped."""
    doc = load_abhi_document(DANGLING)
    snapshot = abhi_to_snapshot(doc, fallback_tenant_id="test", allow_dangling=True)
    # The dangling edge should have been dropped
    from waggle.abhi import _find_dangling_edges, build_abhi_document

    rebuilt_doc = build_abhi_document(snapshot)
    assert _find_dangling_edges(rebuilt_doc) == []


def test_import_dangling_edges_force():
    """abhi_to_snapshot with force=True — no error."""
    doc = load_abhi_document(DANGLING)
    # Should not raise
    snapshot = abhi_to_snapshot(doc, fallback_tenant_id="test", force=True)
    assert snapshot is not None


def test_export_strict_export_rejected():
    """build_abhi_document with strict_export=True and dangling edge — DanglingEdgeError."""
    from uuid import uuid4

    n1 = _node("N1", "Node 1")
    missing_id = str(uuid4())
    e_dangling = _edge(n1["id"], missing_id, "relates_to")
    snapshot = _make_snapshot(nodes=[n1], edges=[e_dangling])

    with pytest.raises(DanglingEdgeError):
        build_abhi_document(snapshot, strict_export=True)


def test_export_strict_export_allowed():
    """build_abhi_document without strict_export=True and dangling edge — no error."""
    from uuid import uuid4

    n1 = _node("N1", "Node 1")
    missing_id = str(uuid4())
    e_dangling = _edge(n1["id"], missing_id, "relates_to")
    snapshot = _make_snapshot(nodes=[n1], edges=[e_dangling])

    # Should not raise — just logs a warning
    doc = build_abhi_document(snapshot)
    assert doc is not None


# ---------------------------------------------------------------------------
# 16.4  Resolve and upgrade tests
# ---------------------------------------------------------------------------


def test_resolve_unknown_conflict_id():
    """resolve_abhi_conflict with a fake conflict_id — ConflictResolutionError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        snap = _make_snapshot(nodes=[_node("N1", "Content")])
        path = Path(tmpdir) / "merged.abhi"
        write_abhi_document(snap, output_path=path)

        with pytest.raises(ConflictResolutionError):
            resolve_abhi_conflict(
                merged_path=path,
                conflict_id="nonexistent-conflict-id",
                resolution="ours",
            )


def test_upgrade_already_at_version(caplog):
    """upgrade_abhi_document with target_version matching current — no file modification."""
    import logging

    with tempfile.TemporaryDirectory() as tmpdir:
        snap = _make_snapshot(nodes=[_node("N1", "Content")])
        path = Path(tmpdir) / "doc.abhi"
        write_abhi_document(snap, output_path=path)

        doc = load_abhi_document(path)
        current_version = doc["manifest"]["schema_version"]
        mtime_before = path.stat().st_mtime

        with caplog.at_level(logging.INFO, logger="waggle.abhi"):
            upgrade_abhi_document(
                doc,
                input_path=path,
                target_version=current_version,
                output_path=path,
            )

        # File should not have been modified
        mtime_after = path.stat().st_mtime
        assert mtime_before == mtime_after
        # Info message should have been logged
        assert any(
            "already at" in record.message.lower() or "no upgrade" in record.message.lower()
            for record in caplog.records
        )


def test_upgrade_unsupported_path():
    """upgrade_abhi_document with unsupported version pair — SchemaVersionError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        snap = _make_snapshot(nodes=[_node("N1", "Content")])
        path = Path(tmpdir) / "doc.abhi"
        write_abhi_document(snap, output_path=path)

        doc = load_abhi_document(path)
        # Patch to v3 (unsupported upgrade target from v2)
        doc["manifest"]["schema_version"] = "2.0.0"

        with pytest.raises(SchemaVersionError):
            upgrade_abhi_document(
                doc,
                input_path=path,
                target_version="99.0.0",
                output_path=path,
            )


@_skip_parser
def test_pull_parser_accepts_drive_merge_strategy():
    assert _build_parser is not None
    assert _normalize_pull_strategy_args is not None

    parser = _build_parser()
    args = _normalize_pull_strategy_args(
        parser.parse_args(
            [
                "pull",
                "remote-file-id",
                "--merge-strategy",
                "prefer_left",
            ]
        )
    )

    assert args.merge_strategy == "prefer_left"
    assert args.import_strategy == "skip-existing"


@_skip_parser
def test_pull_parser_defaults_to_last_write_wins():
    assert _build_parser is not None
    assert _normalize_pull_strategy_args is not None

    parser = _build_parser()
    args = _normalize_pull_strategy_args(parser.parse_args(["pull", "remote-file-id"]))

    assert args.merge_strategy == "last_write_wins"
    assert args.import_strategy == "skip-existing"


@_skip_parser
def test_pull_parser_maps_legacy_merge_strategy_to_import_strategy():
    assert _build_parser is not None
    assert _normalize_pull_strategy_args is not None

    parser = _build_parser()
    parsed_args = parser.parse_args(
        [
            "pull",
            "remote-file-id",
            "--merge-strategy",
            "overwrite",
        ]
    )

    with pytest.warns(FutureWarning, match="deprecated"):
        args = _normalize_pull_strategy_args(parsed_args)

    assert args.merge_strategy == "last_write_wins"
    assert args.import_strategy == "overwrite"


@_skip_parser
def test_explicit_import_strategy_overrides_legacy_merge_value():
    assert _build_parser is not None
    assert _normalize_pull_strategy_args is not None

    parser = _build_parser()
    parsed_args = parser.parse_args(
        [
            "pull",
            "remote-file-id",
            "--merge-strategy",
            "overwrite",
            "--import-strategy",
            "branch",
        ]
    )

    with pytest.warns(FutureWarning, match="ignored"):
        args = _normalize_pull_strategy_args(parsed_args)

    assert args.merge_strategy == "last_write_wins"
    assert args.import_strategy == "branch"


# ---------------------------------------------------------------------------
# 16.5  Exit code and format tests
# ---------------------------------------------------------------------------


@_skip_cli
def test_cli_diff_format_patch_error():
    """waggle diff --format patch — exit code 1 and error message."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        # Create minimal fixture files
        snap = _make_snapshot()
        write_abhi_document(snap, output_path="a.abhi")
        write_abhi_document(snap, output_path="b.abhi")

        result = runner.invoke(cli_app, ["diff", "a.abhi", "b.abhi", "--format", "patch"])
        assert result.exit_code == 1
        assert (
            "patch" in (result.output + (result.stderr if hasattr(result, "stderr") else "")).lower()
            or "not yet supported" in (result.output + "").lower()
            or "patch" in str(result.exception or "").lower()
        )


@_skip_cli
def test_cli_merge_clean_exit_0():
    """Merge identical files — exit code 0."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        snap = _make_snapshot(nodes=[_node("N1", "Content")])
        write_abhi_document(snap, output_path="base.abhi")
        write_abhi_document(snap, output_path="left.abhi")
        write_abhi_document(snap, output_path="right.abhi")

        result = runner.invoke(
            cli_app,
            ["merge", "left.abhi", "right.abhi", "--base", "base.abhi", "--output", "out.abhi"],
        )
        assert result.exit_code == 0


@_skip_cli
def test_cli_merge_conflicts_exit_1():
    """Merge files with conflicts — exit code 1."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        from uuid import uuid4

        node_id = str(uuid4())

        base_node = {
            "id": node_id,
            "label": "Shared Node",
            "content": "Original content",
            "node_type": "fact",
            "tags": [],
            "aliases": [],
            "metadata": {},
        }
        left_node = {**base_node, "content": "Left modified content"}
        right_node = {**base_node, "content": "Right modified content"}

        base_snap = _make_snapshot(nodes=[base_node])
        left_snap = _make_snapshot(nodes=[left_node])
        right_snap = _make_snapshot(nodes=[right_node])

        write_abhi_document(base_snap, output_path="base.abhi")
        write_abhi_document(left_snap, output_path="left.abhi")
        write_abhi_document(right_snap, output_path="right.abhi")

        result = runner.invoke(
            cli_app,
            [
                "merge",
                "left.abhi",
                "right.abhi",
                "--base",
                "base.abhi",
                "--output",
                "out.abhi",
                "--strategy",
                "contradict",
            ],
        )
        assert result.exit_code == 1


@_skip_cli
def test_cli_diff_exit_0():
    """waggle diff two files — exit code 0."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        snap_a = _make_snapshot(nodes=[_node("N1", "Content A")])
        snap_b = _make_snapshot(nodes=[_node("N2", "Content B")])
        write_abhi_document(snap_a, output_path="a.abhi")
        write_abhi_document(snap_b, output_path="b.abhi")

        result = runner.invoke(cli_app, ["diff", "a.abhi", "b.abhi"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# 16.6  Serializer tests
# ---------------------------------------------------------------------------


def test_serialize_abhi_diff_truncation():
    """FieldLevelDiffResult with 60 modified nodes — truncation indicator in output.

    When there are more than 50 changed objects, the serializer either:
    - appends "more changes omitted" summary line, OR
    - truncates the output at max_chars with "[output truncated]"
    Either way, the output must be bounded and indicate truncation.
    """
    from uuid import uuid4

    node_records = []
    nodes_updated = []
    for i in range(60):
        nid = str(uuid4())
        nodes_updated.append(nid)
        node_records.append(
            NodeDiffRecord(
                node_id=nid,
                classification="modified",
                label=f"Node {i}",
                deltas=[FieldDelta(field="content", old_value=f"old {i}", new_value=f"new {i}")],
            )
        )

    result = FieldLevelDiffResult(
        input_path_a="/tmp/a.abhi",
        input_path_b="/tmp/b.abhi",
        nodes_updated=nodes_updated,
        node_records=node_records,
    )

    # Use a large max_chars so the "more changes omitted" line is not cut off
    output = serialize_abhi_diff(result, fmt="human", max_chars=100_000)
    assert "more changes omitted" in output


def test_serialize_abhi_diff_json_format():
    """serialize_abhi_diff with fmt='json' — valid JSON output."""
    from uuid import uuid4

    nid = str(uuid4())
    result = FieldLevelDiffResult(
        input_path_a="/tmp/a.abhi",
        input_path_b="/tmp/b.abhi",
        nodes_added=[nid],
        node_records=[
            NodeDiffRecord(
                node_id=nid,
                classification="added",
                label="Test Node",
                deltas=[],
            )
        ],
    )

    output = serialize_abhi_diff(result, fmt="json")
    parsed = json.loads(output)
    assert isinstance(parsed, dict)
    assert "nodes_added" in parsed
    assert nid in parsed["nodes_added"]


def test_merge_strategy_config_load_missing_file():
    """MergeStrategyConfig.load(path='/nonexistent/path.yaml') — returns default config."""
    config = MergeStrategyConfig.load(path="/nonexistent/path.yaml")
    assert isinstance(config, MergeStrategyConfig)
    assert config.default_strategy == "contradict"
    assert config.field_overrides == []
    assert config.type_overrides == []
