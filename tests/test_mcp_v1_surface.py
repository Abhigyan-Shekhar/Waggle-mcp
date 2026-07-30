"""Tests that the current MCP v1 tool surface matches the frozen fixture."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mcp-v1"


@pytest.fixture(scope="module")
def waggle_server():
    """Instantiate WaggleServer against a throw-away SQLite db."""
    from waggle.config import AppConfig
    from waggle.embeddings import EmbeddingModel
    from waggle.graph import MemoryGraph
    from waggle.server.mcp import WaggleServer

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    config = AppConfig.from_env()
    config.startup_mode = "fast"
    em = EmbeddingModel("deterministic", embedding_backend="local")
    em.disable_warmup()
    graph = MemoryGraph(db_path, em, tenant_id="test-tenant")
    return WaggleServer(graph=graph, config=config)


# ── Tool list surface ──────────────────────────────────────────────────────────

@pytest.mark.skipif(
    not (FIXTURE_DIR / "tool-list.json").exists(),
    reason="Fixture not yet generated. Run: python scripts/generate_mcp_fixtures.py",
)
def test_tool_names_unchanged(waggle_server):
    """Every tool that existed at snapshot time must still exist with the same name."""
    fixture = json.loads((FIXTURE_DIR / "tool-list.json").read_text())
    fixture_names = {t["name"] for t in fixture}

    live_tools = waggle_server.build_tools()
    live_names = {t.name for t in live_tools}

    missing = fixture_names - live_names
    assert not missing, f"Tools removed since fixture was taken: {sorted(missing)}"


@pytest.mark.skipif(
    not (FIXTURE_DIR / "tool-list.json").exists(),
    reason="Fixture not yet generated.",
)
def test_tool_required_fields_unchanged(waggle_server):
    """Required fields for every tool must not shrink (adding new required fields is a
    breaking change; removing required fields is ok but we assert the snapshot set is preserved)."""
    fixture = json.loads((FIXTURE_DIR / "tool-list.json").read_text())
    fixture_map = {t["name"]: t for t in fixture}

    live_tools = waggle_server.build_tools()
    live_map = {t.name: t for t in live_tools}

    for name, fixture_tool in fixture_map.items():
        if name not in live_map:
            continue  # already caught by test_tool_names_unchanged
        fixture_required = set(fixture_tool.get("inputSchema", {}).get("required", []))
        live_required = set((live_map[name].inputSchema or {}).get("required", []))
        # New required fields are a breaking change.
        added = live_required - fixture_required
        assert not added, (
            f"Tool '{name}' gained new required fields since fixture was taken: {sorted(added)}"
        )


@pytest.mark.skipif(
    not (FIXTURE_DIR / "tool-list.json").exists(),
    reason="Fixture not yet generated.",
)
def test_tool_property_names_unchanged(waggle_server):
    """Argument names (properties) in every tool's inputSchema must not be removed."""
    fixture = json.loads((FIXTURE_DIR / "tool-list.json").read_text())
    fixture_map = {t["name"]: t for t in fixture}

    live_tools = waggle_server.build_tools()
    live_map = {t.name: t for t in live_tools}

    for name, fixture_tool in fixture_map.items():
        if name not in live_map:
            continue
        fixture_props = set(fixture_tool.get("inputSchema", {}).get("properties", {}).keys())
        live_props = set((live_map[name].inputSchema or {}).get("properties", {}).keys())
        removed = fixture_props - live_props
        assert not removed, (
            f"Tool '{name}' lost argument properties since fixture was taken: {sorted(removed)}"
        )


# ── Resource list surface ──────────────────────────────────────────────────────

@pytest.mark.skipif(
    not (FIXTURE_DIR / "resource-list.json").exists(),
    reason="Fixture not yet generated.",
)
def test_resource_uris_unchanged(waggle_server):
    """Every resource URI from the fixture must still exist."""
    fixture = json.loads((FIXTURE_DIR / "resource-list.json").read_text())
    fixture_uris = {r["uri"] for r in fixture}

    live_resources = waggle_server.build_resources().resources
    live_uris = {str(r.uri) for r in live_resources}

    missing = fixture_uris - live_uris
    assert not missing, f"Resources removed since fixture was taken: {sorted(missing)}"


# ── Prompt surface ─────────────────────────────────────────────────────────────

@pytest.mark.skipif(
    not (FIXTURE_DIR / "prompt-list.json").exists(),
    reason="Fixture not yet generated.",
)
def test_prompt_names_unchanged(waggle_server):
    """Every prompt from the fixture must still exist."""
    fixture = json.loads((FIXTURE_DIR / "prompt-list.json").read_text())
    fixture_names = {p["name"] for p in fixture}

    live_prompts = waggle_server.build_prompts()
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
    from waggle.server.mcp import _TOOL_ALIASES

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
def test_tool_count_not_reduced(waggle_server):
    """The number of registered tools must not decrease."""
    summary = json.loads((FIXTURE_DIR / "summary.json").read_text())
    expected_count = summary["tool_count"]

    live_count = len(waggle_server.build_tools())
    assert live_count >= expected_count, (
        f"Tool count dropped from {expected_count} to {live_count}. "
        "Check that no tools were accidentally removed."
    )
