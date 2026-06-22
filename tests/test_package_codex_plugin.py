from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

from scripts.build_codex_plugin_runtime import TARGETS
from scripts.package_codex_plugin import package_release, validate_bundle_inputs


def test_package_release_emits_marketplace_and_plugin_archives(tmp_path: Path) -> None:
    root = _make_fake_codex_plugin_tree(tmp_path)
    output_dir = tmp_path / "dist"

    created_files = package_release(root, output_dir, "v9.9.9")
    created_names = sorted(path.name for path in created_files)

    assert created_names == [
        "waggle-codex-marketplace-v9.9.9.zip",
        "waggle-codex-marketplace-v9.9.9.zip.sha256",
        "waggle-codex-plugin-v9.9.9.zip",
        "waggle-codex-plugin-v9.9.9.zip.sha256",
    ]

    plugin_entries = _zip_entries(output_dir / "waggle-codex-plugin-v9.9.9.zip")
    assert "waggle-codex-plugin-v9.9.9/.codex-plugin/plugin.json" in plugin_entries
    assert "waggle-codex-plugin-v9.9.9/.mcp.json" in plugin_entries
    assert "waggle-codex-plugin-v9.9.9/bin/waggle-server-launcher.js" in plugin_entries
    assert "waggle-codex-plugin-v9.9.9/runtime/darwin-arm64/waggle-server" in plugin_entries
    assert "waggle-codex-plugin-v9.9.9/INSTALL.md" in plugin_entries
    assert all(not entry.endswith(".gitkeep") for entry in plugin_entries)

    marketplace_entries = _zip_entries(output_dir / "waggle-codex-marketplace-v9.9.9.zip")
    assert "waggle-codex-marketplace-v9.9.9/.agents/plugins/marketplace.json" in marketplace_entries
    assert "waggle-codex-marketplace-v9.9.9/.codex-plugin/plugin.json" in marketplace_entries
    assert "waggle-codex-marketplace-v9.9.9/.mcp.json" in marketplace_entries
    assert "waggle-codex-marketplace-v9.9.9/plugins/waggle/.codex-plugin/plugin.json" in marketplace_entries
    assert (
        "waggle-codex-marketplace-v9.9.9/plugins/waggle/runtime/win32-x86_64/waggle-server.exe" in marketplace_entries
    )
    assert "waggle-codex-marketplace-v9.9.9/INSTALL.md" in marketplace_entries
    assert all(not entry.endswith(".gitkeep") for entry in marketplace_entries)


def test_validate_bundle_inputs_reports_missing_runtime_binary(tmp_path: Path) -> None:
    root = _make_fake_codex_plugin_tree(tmp_path)
    missing_binary = root / "plugins" / "waggle" / "runtime" / "linux-x86_64" / "waggle-server"
    missing_binary.unlink()

    failures = validate_bundle_inputs(root)

    assert any("linux-x86_64/waggle-server" in failure for failure in failures)


def _make_fake_codex_plugin_tree(root: Path) -> Path:
    (root / ".agents" / "plugins").mkdir(parents=True)
    (root / ".codex-plugin").mkdir()
    (root / "plugins" / "waggle" / ".codex-plugin").mkdir(parents=True)
    (root / "plugins" / "waggle" / "bin").mkdir(parents=True)
    (root / "plugins" / "waggle" / "runtime").mkdir(parents=True)

    marketplace_payload = {
        "name": "local-repo",
        "interface": {"displayName": "Local Repo"},
        "plugins": [
            {
                "name": "waggle",
                "source": {"source": "local", "path": "./plugins/waggle"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity",
            }
        ],
    }

    (root / ".agents" / "plugins" / "marketplace.json").write_text(json.dumps(marketplace_payload))
    (root / ".codex-plugin" / "plugin.json").write_text('{"name":"waggle"}')
    (root / ".mcp.json").write_text('{"mcpServers":{"waggle":{"command":"node"}}}')
    (root / "plugins" / "waggle" / ".codex-plugin" / "plugin.json").write_text('{"name":"waggle"}')
    (root / "plugins" / "waggle" / ".mcp.json").write_text('{"mcpServers":{"waggle":{"command":"node"}}}')
    (root / "plugins" / "waggle" / "bin" / "waggle-server-launcher.js").write_text("console.log('waggle');\n")
    (root / "plugins" / "waggle" / "runtime" / "README.md").write_text("# Runtime\n")

    for target, executable in TARGETS.items():
        target_dir = root / "plugins" / "waggle" / "runtime" / target
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / executable).write_bytes(b"binary")
        (target_dir / ".gitkeep").write_text("")

    return root


def _zip_entries(path: Path) -> set[str]:
    with ZipFile(path) as archive:
        return set(archive.namelist())
