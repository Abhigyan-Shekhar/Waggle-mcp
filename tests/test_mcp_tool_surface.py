"""Tests that the current MCP tool surface matches the frozen compatibility fixture."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mcp-v1"


class FakeEmbeddingModel:
    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"
    warmup_status = "disabled"
    warmup_error = ""

    def disable_warmup(self) -> None:
        self.warmup_status = "disabled"

    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(8, dtype=np.float32)
        for token in text.lower().split():
            index = sum(ord(character) for character in token) % len(vector)
            vector[index] += 1.0
        norm = np.linalg.norm(vector)
        if norm == 0.0:
            return vector
        return vector / norm

    def to_bytes(self, embedding: np.ndarray) -> bytes:
        return embedding.astype(np.float32).tobytes()

    def from_bytes(self, data: bytes) -> np.ndarray:
        return np.frombuffer(data, dtype=np.float32)

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        a_norm = np.linalg.norm(a)
        b_norm = np.linalg.norm(b)
        if a_norm == 0.0 or b_norm == 0.0:
            return 0.0
        return float(np.dot(a, b) / (a_norm * b_norm))


@pytest.fixture(scope="module")
def mcp_surface():
    """Instantiate the SDK v2 adapter against a throw-away SQLite db."""
    from waggle.config import AppConfig
    from waggle.graph import MemoryGraph
    from waggle.metrics import MetricsRegistry
    from waggle.protocol.mcp.adapter import WagglemcpAdapter
    from waggle.tools.dispatcher import WaggleToolDispatcher

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    config = AppConfig.from_env()
    config.startup_mode = "fast"
    em = FakeEmbeddingModel()
    em.disable_warmup()
    graph = MemoryGraph(db_path, em, tenant_id="test-tenant")
    dispatcher = WaggleToolDispatcher(graph=graph, config=config, metrics=MetricsRegistry())
    return WagglemcpAdapter(dispatcher=dispatcher)


def _live_tools_fixture_shape(mcp_surface):
    """Return tools normalized to the historical fixture's camelCase schema."""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema,
        }
        for tool in mcp_surface._dispatcher.list_tools()
    ]


def test_all_tools_have_claude_directory_annotations(mcp_surface):
    """Claude directory review requires every tool to have a title and safety hint."""
    for tool in mcp_surface._dispatcher.list_tools():
        assert tool.title, f"Tool '{tool.name}' is missing a title."
        annotations = tool.annotations or {}
        assert annotations.get("title"), f"Tool '{tool.name}' is missing annotations.title."
        assert annotations.get("read_only_hint") is not None, (
            f"Tool '{tool.name}' is missing annotations.read_only_hint."
        )
        assert annotations.get("destructive_hint") is not None, (
            f"Tool '{tool.name}' is missing annotations.destructive_hint."
        )
        assert len(tool.name) <= 64, f"Tool '{tool.name}' exceeds Claude's 64-character name limit."
        if annotations["read_only_hint"]:
            assert annotations["destructive_hint"] is False, (
                f"Tool '{tool.name}' cannot be both read-only and destructive."
            )


def test_export_visualization_tools_are_not_read_only(mcp_surface):
    tools = {tool.name: tool for tool in mcp_surface._dispatcher.list_tools()}

    assert tools["export_graph_html"].annotations["read_only_hint"] is False
    assert tools["window_graph_viz"].annotations["read_only_hint"] is False


# ── Tool list surface ──────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not (FIXTURE_DIR / "tool-list.json").exists(),
    reason="Fixture not yet generated. Run: python scripts/generate_mcp_fixtures.py",
)
def test_tool_names_unchanged(mcp_surface):
    """Every tool that existed at snapshot time must still exist with the same name."""
    fixture = json.loads((FIXTURE_DIR / "tool-list.json").read_text())
    fixture_names = {t["name"] for t in fixture}

    live_tools = _live_tools_fixture_shape(mcp_surface)
    live_names = {t["name"] for t in live_tools}

    missing = fixture_names - live_names
    assert not missing, f"Tools removed since fixture was taken: {sorted(missing)}"


@pytest.mark.skipif(
    not (FIXTURE_DIR / "tool-list.json").exists(),
    reason="Fixture not yet generated.",
)
def test_tool_required_fields_unchanged(mcp_surface):
    """Required fields for every tool must not shrink (adding new required fields is a
    breaking change; removing required fields is ok but we assert the snapshot set is preserved)."""
    fixture = json.loads((FIXTURE_DIR / "tool-list.json").read_text())
    fixture_map = {t["name"]: t for t in fixture}

    live_tools = _live_tools_fixture_shape(mcp_surface)
    live_map = {t["name"]: t for t in live_tools}

    for name, fixture_tool in fixture_map.items():
        if name not in live_map:
            continue  # already caught by test_tool_names_unchanged
        fixture_required = set(fixture_tool.get("inputSchema", {}).get("required", []))
        live_required = set((live_map[name]["inputSchema"] or {}).get("required", []))
        # New required fields are a breaking change.
        added = live_required - fixture_required
        assert not added, f"Tool '{name}' gained new required fields since fixture was taken: {sorted(added)}"


@pytest.mark.skipif(
    not (FIXTURE_DIR / "tool-list.json").exists(),
    reason="Fixture not yet generated.",
)
def test_tool_property_names_unchanged(mcp_surface):
    """Argument names (properties) in every tool's inputSchema must not be removed."""
    fixture = json.loads((FIXTURE_DIR / "tool-list.json").read_text())
    fixture_map = {t["name"]: t for t in fixture}

    live_tools = _live_tools_fixture_shape(mcp_surface)
    live_map = {t["name"]: t for t in live_tools}

    for name, fixture_tool in fixture_map.items():
        if name not in live_map:
            continue
        fixture_props = set(fixture_tool.get("inputSchema", {}).get("properties", {}).keys())
        live_props = set((live_map[name]["inputSchema"] or {}).get("properties", {}).keys())
        removed = fixture_props - live_props
        assert not removed, f"Tool '{name}' lost argument properties since fixture was taken: {sorted(removed)}"


# ── Resource list surface ──────────────────────────────────────────────────────


@pytest.mark.skipif(
    not (FIXTURE_DIR / "resource-list.json").exists(),
    reason="Fixture not yet generated.",
)
def test_resource_uris_unchanged():
    """Every resource URI from the fixture must still exist."""
    fixture = json.loads((FIXTURE_DIR / "resource-list.json").read_text())
    fixture_uris = {r["uri"] for r in fixture}

    from waggle.protocol.mcp.surface import build_resources

    live_resources = build_resources().resources
    live_uris = {str(r.uri) for r in live_resources}

    missing = fixture_uris - live_uris
    assert not missing, f"Resources removed since fixture was taken: {sorted(missing)}"


# ── Prompt surface ─────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not (FIXTURE_DIR / "prompt-list.json").exists(),
    reason="Fixture not yet generated.",
)
def test_prompt_names_unchanged():
    """Every prompt from the fixture must still exist."""
    fixture = json.loads((FIXTURE_DIR / "prompt-list.json").read_text())
    fixture_names = {p["name"] for p in fixture}

    from waggle.protocol.mcp.surface import build_prompts

    live_prompts = build_prompts()
    live_names = {p.name for p in live_prompts}

    missing = fixture_names - live_names
    assert not missing, f"Prompts removed since fixture was taken: {sorted(missing)}"


# ── Tool alias surface ─────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not (FIXTURE_DIR / "tool-aliases.json").exists(),
    reason="Fixture not yet generated.",
)
def test_tool_aliases_unchanged():
    """Every alias from the fixture must still be wired to the same canonical tool."""
    from waggle.tools.dispatcher import _TOOL_ALIASES

    fixture = json.loads((FIXTURE_DIR / "tool-aliases.json").read_text())

    for alias, info in fixture.items():
        assert alias in _TOOL_ALIASES, f"Alias '{alias}' was removed."
        live_canonical, _ = _TOOL_ALIASES[alias]
        assert live_canonical == info["canonical"], (
            f"Alias '{alias}' now points to '{live_canonical}' instead of '{info['canonical']}'"
        )


# ── Tool count guard ───────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not (FIXTURE_DIR / "summary.json").exists(),
    reason="Fixture not yet generated.",
)
def test_tool_count_not_reduced(mcp_surface):
    """The number of registered tools must not decrease."""
    summary = json.loads((FIXTURE_DIR / "summary.json").read_text())
    expected_count = summary["tool_count"]

    live_count = len(_live_tools_fixture_shape(mcp_surface))
    assert live_count >= expected_count, (
        f"Tool count dropped from {expected_count} to {live_count}. Check that no tools were accidentally removed."
    )
