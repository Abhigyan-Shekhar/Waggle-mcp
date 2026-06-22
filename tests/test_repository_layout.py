from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_moved_contributor_files_do_not_reappear_at_repo_root() -> None:
    old_root_files = [
        "COMMERCIAL.md",
        "TRAVERSAL_VERIFICATION.md",
        "build_clean_oolong_dataset.py",
        "check_overfitting.py",
        "codex_config.example.toml",
        "generate_oolong_dataset.py",
        "patch_aggregate.py",
    ]

    for filename in old_root_files:
        assert not (ROOT / filename).exists(), f"{filename} should not live at the repo root"


def test_moved_contributor_files_exist_in_their_canonical_locations() -> None:
    canonical_paths = [
        ROOT / "examples" / "antigravity_mcp_config.example.json",
        ROOT / "docs" / "commercial.md",
        ROOT / "examples" / "cursor_mcp.example.json",
        ROOT / "docs" / "traversal-verification.md",
        ROOT / "examples" / "codex_config.example.toml",
        ROOT / "scripts" / "oolong" / "build_clean_oolong_dataset.py",
        ROOT / "scripts" / "oolong" / "check_overfitting.py",
        ROOT / "scripts" / "oolong" / "generate_oolong_dataset.py",
        ROOT / "scripts" / "verification" / "patch_aggregate.py",
    ]

    for path in canonical_paths:
        assert path.exists(), f"Missing canonical file: {path.relative_to(ROOT)}"


def test_registry_and_distribution_manifests_remain_at_repo_root() -> None:
    required_root_files = [
        ".codex-plugin/plugin.json",
        ".mcp.json",
        "glama.json",
        "llms-install.txt",
        "server.json",
        "smithery.yaml",
    ]

    for filename in required_root_files:
        assert (ROOT / filename).exists(), f"{filename} must remain at the repo root"


def test_codex_repo_marketplace_and_plugin_entry_exist() -> None:
    expected_paths = [
        ROOT / ".agents" / "plugins" / "marketplace.json",
        ROOT / "plugins" / "waggle" / ".codex-plugin" / "plugin.json",
        ROOT / "plugins" / "waggle" / ".mcp.json",
    ]

    for path in expected_paths:
        assert path.exists(), f"Missing Codex marketplace path: {path.relative_to(ROOT)}"


def test_codex_plugin_uses_plugin_local_launcher() -> None:
    for manifest_path in [ROOT / ".mcp.json", ROOT / "plugins" / "waggle" / ".mcp.json"]:
        payload = json.loads(manifest_path.read_text())
        server = payload["mcpServers"]["waggle"]

        assert server["command"] == "node"
        assert any("waggle-server-launcher.js" in arg for arg in server["args"])
        assert server["args"][-3:] == ["serve", "--transport", "stdio"]
        assert server["command"] != "waggle-mcp"


def test_codex_plugin_runtime_layout_is_declared() -> None:
    expected_targets = {
        "darwin-arm64": "waggle-server",
        "darwin-x86_64": "waggle-server",
        "linux-x86_64": "waggle-server",
        "linux-aarch64": "waggle-server",
        "win32-x86_64": "waggle-server.exe",
    }

    launcher = ROOT / "plugins" / "waggle" / "bin" / "waggle-server-launcher.js"
    assert launcher.exists(), "Missing Codex plugin runtime launcher"

    runtime_root = ROOT / "plugins" / "waggle" / "runtime"
    for target in expected_targets:
        assert (runtime_root / target).is_dir(), f"Missing runtime target directory: {target}"


def test_app_surfaces_live_under_apps_directory() -> None:
    expected_paths = [
        ROOT / "apps" / "README.md",
        ROOT / "apps" / "mcp" / "graph-ui" / "package.json",
        ROOT / "apps" / "mcp" / "claude-desktop-extension" / "manifest.json",
        ROOT / "apps" / "vscode-extension" / "package.json",
    ]

    for path in expected_paths:
        assert path.exists(), f"Missing app surface path: {path.relative_to(ROOT)}"
