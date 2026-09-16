#!/usr/bin/env python3
"""
Generate MCP tool surface compatibility fixtures.

Run from the repo root:
    uv run python scripts/generate_mcp_fixtures.py

Writes files into tests/fixtures/mcp-v1/ for historical compatibility.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make sure we're running from the repo root with the editable install active.
ROOT = Path(__file__).parent.parent
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "mcp-v1"

sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    # Import after path is set up.
    from waggle.config import AppConfig
    from waggle.embeddings import EmbeddingModel
    from waggle.graph import MemoryGraph
    from waggle.metrics import MetricsRegistry
    from waggle.protocol.mcp.surface import build_prompts, build_resources
    from waggle.tools.dispatcher import _TOOL_ALIASES, WaggleToolDispatcher

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    # Build a minimal in-memory graph (deterministic embeddings, temp db).
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    config = AppConfig.from_env()
    # Override to deterministic/fast settings for fixture generation.
    config.startup_mode = "fast"
    em = EmbeddingModel("deterministic", embedding_backend="local")
    em.disable_warmup()
    graph = MemoryGraph(
        db_path,
        em,
        tenant_id="fixture-tenant",
    )

    dispatcher = WaggleToolDispatcher(graph=graph, config=config, metrics=MetricsRegistry())

    # ── Tool list ─────────────────────────────────────────────────────────
    tools = dispatcher.list_tools()
    tool_list = []
    for tool in tools:
        tool_list.append(
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
            }
        )

    tool_list_path = FIXTURE_DIR / "tool-list.json"
    tool_list_path.write_text(json.dumps(tool_list, indent=2))
    print(f"Wrote {len(tool_list)} tools → {tool_list_path}")

    # ── Resource list ─────────────────────────────────────────────────────
    resources_result = build_resources()
    resource_list = []
    for r in resources_result.resources:
        resource_list.append(
            {
                "uri": str(r.uri),
                "name": r.name,
                "description": r.description,
                "mimeType": r.mime_type,
            }
        )

    resource_list_path = FIXTURE_DIR / "resource-list.json"
    resource_list_path.write_text(json.dumps(resource_list, indent=2))
    print(f"Wrote {len(resource_list)} resources → {resource_list_path}")

    # ── Prompt list ───────────────────────────────────────────────────────
    prompts = build_prompts()
    prompt_list = []
    for p in prompts:
        prompt_list.append(
            {
                "name": p.name,
                "description": p.description,
                "arguments": [
                    {
                        "name": arg.name,
                        "description": arg.description,
                        "required": arg.required,
                    }
                    for arg in (p.arguments or [])
                ],
            }
        )

    prompt_list_path = FIXTURE_DIR / "prompt-list.json"
    prompt_list_path.write_text(json.dumps(prompt_list, indent=2))
    print(f"Wrote {len(prompt_list)} prompts → {prompt_list_path}")

    # ── Tool aliases ──────────────────────────────────────────────────────
    alias_map = {
        alias: {"canonical": canonical, "default_args": defaults}
        for alias, (canonical, defaults) in _TOOL_ALIASES.items()
    }
    alias_path = FIXTURE_DIR / "tool-aliases.json"
    alias_path.write_text(json.dumps(alias_map, indent=2))
    print(f"Wrote {len(alias_map)} aliases → {alias_path}")

    # ── Summary ───────────────────────────────────────────────────────────
    summary = {
        "tool_count": len(tool_list),
        "resource_count": len(resource_list),
        "prompt_count": len(prompt_list),
        "alias_count": len(alias_map),
        "tool_names": [t["name"] for t in tool_list],
    }
    summary_path = FIXTURE_DIR / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"\nSummary → {summary_path}")
    print(f"Tool names: {', '.join(summary['tool_names'])}")


if __name__ == "__main__":
    main()
