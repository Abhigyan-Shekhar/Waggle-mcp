from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.build_codex_plugin_runtime import TARGETS
from scripts.package_codex_plugin import CODEX_SKILLS, package_release, validate_bundle_inputs

ROOT = Path(__file__).resolve().parents[1]


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
        "waggle-codex-release-v9.9.9.json",
    ]

    plugin_entries = _zip_entries(output_dir / "waggle-codex-plugin-v9.9.9.zip")
    assert "waggle-codex-plugin-v9.9.9/.codex-plugin/plugin.json" in plugin_entries
    assert "waggle-codex-plugin-v9.9.9/.mcp.json" in plugin_entries
    assert "waggle-codex-plugin-v9.9.9/bin/waggle-server-launcher.js" in plugin_entries
    assert "waggle-codex-plugin-v9.9.9/skills/waggle-memory/SKILL.md" in plugin_entries
    assert "waggle-codex-plugin-v9.9.9/skills/waggle-prime/SKILL.md" in plugin_entries
    assert "waggle-codex-plugin-v9.9.9/skills/waggle-recall/SKILL.md" in plugin_entries
    assert "waggle-codex-plugin-v9.9.9/skills/waggle-checkpoint/SKILL.md" in plugin_entries
    assert "waggle-codex-plugin-v9.9.9/runtime/darwin-arm64/waggle-server" in plugin_entries
    assert "waggle-codex-plugin-v9.9.9/INSTALL.md" in plugin_entries
    assert all(not entry.endswith(".gitkeep") for entry in plugin_entries)
    assert _zip_mode(output_dir / "waggle-codex-plugin-v9.9.9.zip", "runtime/darwin-arm64/waggle-server") & 0o111
    plugin_install = _zip_text(
        output_dir / "waggle-codex-plugin-v9.9.9.zip",
        "waggle-codex-plugin-v9.9.9/INSTALL.md",
    )
    assert "local stdio MCP server" in plugin_install
    assert "paid Apple/Windows signing certificates" in plugin_install
    assert "unsigned" in plugin_install

    marketplace_entries = _zip_entries(output_dir / "waggle-codex-marketplace-v9.9.9.zip")
    assert "waggle-codex-marketplace-v9.9.9/.agents/plugins/marketplace.json" in marketplace_entries
    assert "waggle-codex-marketplace-v9.9.9/.codex-plugin/plugin.json" in marketplace_entries
    assert "waggle-codex-marketplace-v9.9.9/.mcp.json" in marketplace_entries
    assert "waggle-codex-marketplace-v9.9.9/skills/waggle-memory/SKILL.md" in marketplace_entries
    assert "waggle-codex-marketplace-v9.9.9/plugins/waggle/.codex-plugin/plugin.json" in marketplace_entries
    assert (
        "waggle-codex-marketplace-v9.9.9/plugins/waggle/runtime/win32-x86_64/waggle-server.exe" in marketplace_entries
    )
    assert "waggle-codex-marketplace-v9.9.9/INSTALL.md" in marketplace_entries
    assert all(not entry.endswith(".gitkeep") for entry in marketplace_entries)
    assert (
        _zip_mode(
            output_dir / "waggle-codex-marketplace-v9.9.9.zip",
            "plugins/waggle/runtime/linux-x86_64/waggle-server",
        )
        & 0o111
    )
    marketplace_install = _zip_text(
        output_dir / "waggle-codex-marketplace-v9.9.9.zip",
        "waggle-codex-marketplace-v9.9.9/INSTALL.md",
    )
    assert "self-hosted through GitHub Releases" in marketplace_install
    assert "intentionally unsigned" in marketplace_install
    assert "No paid hosted backend" in marketplace_install

    release_manifest = json.loads((output_dir / "waggle-codex-release-v9.9.9.json").read_text())
    assert release_manifest["distribution"] == "single-bundle"
    assert release_manifest["plugin_version"] == "9.9.9"
    assert release_manifest["platform_artifact_resolution"] == "not-supported-by-current-codex-marketplace-schema"
    assert {artifact["name"] for artifact in release_manifest["artifacts"]} == {
        "waggle-codex-marketplace-v9.9.9.zip",
        "waggle-codex-marketplace-v9.9.9.zip.sha256",
        "waggle-codex-plugin-v9.9.9.zip",
        "waggle-codex-plugin-v9.9.9.zip.sha256",
    }


def test_validate_bundle_inputs_reports_missing_runtime_binary(tmp_path: Path) -> None:
    root = _make_fake_codex_plugin_tree(tmp_path)
    missing_binary = root / "plugins" / "waggle" / "runtime" / "linux-x86_64" / "waggle-server"
    missing_binary.unlink()

    failures = validate_bundle_inputs(root)

    assert any("linux-x86_64/waggle-server" in failure for failure in failures)


def test_package_release_reports_missing_root_manifest_cleanly(tmp_path: Path) -> None:
    root = _make_fake_codex_plugin_tree(tmp_path)
    (root / ".codex-plugin" / "plugin.json").unlink()

    with pytest.raises(SystemExit) as exc_info:
        package_release(root, tmp_path / "dist", "v9.9.9")

    assert "Missing required bundle file: .codex-plugin/plugin.json" in str(exc_info.value)


def test_validate_bundle_inputs_reports_plugin_version_drift(tmp_path: Path) -> None:
    root = _make_fake_codex_plugin_tree(tmp_path)
    (root / "plugins" / "waggle" / ".codex-plugin" / "plugin.json").write_text('{"name":"waggle","version":"9.9.8"}')

    failures = validate_bundle_inputs(root)

    assert any("does not match Codex plugin version" in failure for failure in failures)


def test_validate_bundle_inputs_reports_plugin_version_downgrade(tmp_path: Path) -> None:
    root = _make_fake_codex_plugin_tree(tmp_path)
    (root / ".codex-plugin" / "plugin.json").write_text('{"name":"waggle","version":"0.0.9"}')
    (root / "plugins" / "waggle" / ".codex-plugin" / "plugin.json").write_text('{"name":"waggle","version":"0.0.9"}')

    failures = validate_bundle_inputs(root)

    assert any("would downgrade the published Codex plugin version" in failure for failure in failures)


def test_validate_bundle_inputs_rejects_direct_mcp_map_for_current_codex_validator(tmp_path: Path) -> None:
    root = _make_fake_codex_plugin_tree(tmp_path)
    (root / "plugins" / "waggle" / ".mcp.json").write_text('{"waggle":{"command":"node"}}')

    failures = validate_bundle_inputs(root)

    assert any("Codex-compatible mcpServers map" in failure for failure in failures)


def test_validate_bundle_inputs_requires_codex_skills(tmp_path: Path) -> None:
    root = _make_fake_codex_plugin_tree(tmp_path)
    (root / "plugins" / "waggle" / "skills" / "waggle-recall" / "SKILL.md").unlink()

    failures = validate_bundle_inputs(root)

    assert any("skills/waggle-recall/SKILL.md" in failure for failure in failures)


def test_waggle_memory_skill_has_explicit_storage_threshold() -> None:
    root_skill = (ROOT / "skills" / "waggle-memory" / "SKILL.md").read_text()
    standalone_skill = (ROOT / "plugins" / "waggle" / "skills" / "waggle-memory" / "SKILL.md").read_text()

    assert root_skill == standalone_skill
    assert "forgetting it would likely cause duplicated work" in root_skill
    assert "a wrong future decision" in root_skill
    assert "violation of an established constraint" in root_skill
    assert "Do not store something merely because it happened" in root_skill


def test_codex_plugin_uses_portable_offline_safe_startup() -> None:
    root_server = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["waggle"]
    standalone_server = json.loads((ROOT / "plugins" / "waggle" / ".mcp.json").read_text())["mcpServers"]["waggle"]
    assert root_server["cwd"] == "./plugins/waggle"
    assert standalone_server["cwd"] == "."

    for server in (root_server, standalone_server):
        assert server["env"]["WAGGLE_MODEL"] == "deterministic"
        assert server["env"]["WAGGLE_STARTUP_MODE"] == "normal"

    launcher = (ROOT / "plugins" / "waggle" / "bin" / "waggle-server-launcher.js").read_text()
    assert 'WAGGLE_MODEL: "deterministic"' in launcher
    assert 'WAGGLE_STARTUP_MODE: "normal"' in launcher


def _make_fake_codex_plugin_tree(root: Path) -> Path:
    (root / ".agents" / "plugins").mkdir(parents=True)
    (root / ".codex-plugin").mkdir()
    (root / "plugins" / "waggle" / ".codex-plugin").mkdir(parents=True)
    (root / "plugins" / "waggle" / "bin").mkdir(parents=True)
    (root / "plugins" / "waggle" / "runtime").mkdir(parents=True)

    marketplace_payload = {
        "name": "waggle",
        "interface": {"displayName": "Waggle"},
        "plugins": [
            {
                "name": "waggle",
                "source": {"source": "local", "path": "./plugins/waggle"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Developer Tools",
            }
        ],
    }

    (root / ".agents" / "plugins" / "marketplace.json").write_text(json.dumps(marketplace_payload))
    (root / "pyproject.toml").write_text('[project]\nname = "waggle-mcp"\nversion = "9.9.9"\n')
    (root / ".codex-plugin" / "plugin.json").write_text(
        '{"name":"waggle","version":"9.9.9","skills":"./skills/","mcpServers":"./.mcp.json"}'
    )
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "waggle": {
                        "command": "node",
                        "cwd": "./plugins/waggle",
                        "args": [
                            "./plugins/waggle/bin/waggle-server-launcher.js",
                            "serve",
                            "--transport",
                            "stdio",
                        ],
                        "env": {
                            "WAGGLE_BACKEND": "sqlite",
                            "WAGGLE_MODEL": "deterministic",
                            "WAGGLE_STARTUP_MODE": "normal",
                            "WAGGLE_TRANSPORT": "stdio",
                        },
                    }
                }
            }
        )
    )
    (root / "plugins" / "waggle" / ".codex-plugin" / "plugin.json").write_text(
        '{"name":"waggle","version":"9.9.9","skills":"./skills/","mcpServers":"./.mcp.json"}'
    )
    (root / "plugins" / "waggle" / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "waggle": {
                        "command": "node",
                        "cwd": ".",
                        "args": ["./bin/waggle-server-launcher.js", "serve", "--transport", "stdio"],
                        "env": {
                            "WAGGLE_BACKEND": "sqlite",
                            "WAGGLE_MODEL": "deterministic",
                            "WAGGLE_STARTUP_MODE": "normal",
                            "WAGGLE_TRANSPORT": "stdio",
                        },
                    }
                }
            }
        )
    )
    (root / "plugins" / "waggle" / "bin" / "waggle-server-launcher.js").write_text("console.log('waggle');\n")
    (root / "plugins" / "waggle" / "runtime" / "README.md").write_text("# Runtime\n")

    for skill_name in CODEX_SKILLS:
        for skills_root in (root / "skills", root / "plugins" / "waggle" / "skills"):
            skill_dir = skills_root / skill_name
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text(
                f"---\nname: {skill_name}\ndescription: Test skill.\n---\n\nTest instructions.\n"
            )

    for target, executable in TARGETS.items():
        target_dir = root / "plugins" / "waggle" / "runtime" / target
        target_dir.mkdir(parents=True, exist_ok=True)
        binary = target_dir / executable
        binary.write_bytes(b"binary")
        if not target.startswith("win32-"):
            binary.chmod(0o755)
        (target_dir / ".gitkeep").write_text("")

    return root


def _zip_entries(path: Path) -> set[str]:
    with ZipFile(path) as archive:
        return set(archive.namelist())


def _zip_text(path: Path, name: str) -> str:
    with ZipFile(path) as archive:
        return archive.read(name).decode()


def _zip_mode(path: Path, suffix: str) -> int:
    with ZipFile(path) as archive:
        matches = [name for name in archive.namelist() if name.endswith(suffix)]
        assert len(matches) == 1
        return (archive.getinfo(matches[0]).external_attr >> 16) & 0o777
