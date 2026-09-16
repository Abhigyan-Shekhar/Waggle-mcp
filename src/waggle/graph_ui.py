from __future__ import annotations

import json
from html import escape
from pathlib import Path


def render_graph_editor_html(
    *,
    mode: str = "edit",
    project: str = "",
    agent_id: str = "",
    session_id: str = "",
    title: str = "Waggle",
    demo_mode: bool = False,
) -> str:
    page_mode = "view" if mode.strip().lower() == "view" else "edit"
    assets_dir = Path(__file__).resolve().parent / "static" / "graph"
    try:
        asset_version = int(max((assets_dir / "app.css").stat().st_mtime, (assets_dir / "app.js").stat().st_mtime))
    except FileNotFoundError:
        asset_version = 0
    config = json.dumps(
        {
            "schemaVersion": 1,
            "mode": page_mode,
            "sampleMode": False,
            "demoMode": bool(demo_mode),
            "scope": {
                "project": project,
                "agent_id": agent_id,
                "session_id": session_id,
            },
            "project": project,
            "agent_id": agent_id,
            "session_id": session_id,
        }
    )
    # JSON is embedded in a raw-text script, where HTML entities would not decode.
    # Escape HTML delimiters as JSON Unicode escapes to retain the original values.
    config = config.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <link rel="stylesheet" href="/graph-assets/app.css?v={asset_version}">
</head>
<body>
  <div id="root"></div>
  <script>
    window.__WAGGLE_GRAPH_CONFIG__ = {config};
  </script>
  <script type="module" src="/graph-assets/app.js?v={asset_version}"></script>
</body>
</html>"""
