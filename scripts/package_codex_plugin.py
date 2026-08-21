from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_codex_plugin_runtime import TARGETS  # noqa: E402

PLUGIN_DIR = Path("plugins") / "waggle"
FIXED_TIMESTAMP = (2000, 1, 1, 0, 0, 0)

ROOT_BUNDLE_FILES = [
    Path(".agents") / "plugins" / "marketplace.json",
    Path(".codex-plugin") / "plugin.json",
    Path(".mcp.json"),
]

PLUGIN_BUNDLE_FILES = [
    Path(".codex-plugin") / "plugin.json",
    Path(".mcp.json"),
    Path("bin") / "waggle-server-launcher.js",
    Path("runtime") / "README.md",
]

MAX_RELEASE_BUNDLE_BYTES = 450 * 1024 * 1024
MARKETPLACE_DISTRIBUTION = "single-bundle"
MIN_CODEX_PLUGIN_VERSION = "0.1.0"
CODEX_SKILLS = (
    "waggle-memory",
    "waggle-prime",
    "waggle-recall",
    "waggle-checkpoint",
)


def validate_bundle_inputs(root: Path) -> list[str]:
    failures: list[str] = []

    for relative_path in ROOT_BUNDLE_FILES:
        if not (root / relative_path).exists():
            failures.append(f"Missing required bundle file: {relative_path.as_posix()}")

    plugin_root = root / PLUGIN_DIR
    for relative_path in PLUGIN_BUNDLE_FILES:
        if not (plugin_root / relative_path).exists():
            failures.append(f"Missing required plugin file: {(PLUGIN_DIR / relative_path).as_posix()}")

    for target, executable in TARGETS.items():
        binary = plugin_root / "runtime" / target / executable
        if not binary.exists():
            failures.append(f"Missing runtime binary for bundle: {binary.relative_to(root).as_posix()}")

    marketplace_path = root / ".agents" / "plugins" / "marketplace.json"
    if marketplace_path.exists():
        payload = json.loads(marketplace_path.read_text())
        plugins = payload.get("plugins", [])
        waggle_entry = next((plugin for plugin in plugins if plugin.get("name") == "waggle"), None)
        if waggle_entry is None:
            failures.append(".agents/plugins/marketplace.json is missing the waggle plugin entry")
        else:
            source_path = waggle_entry.get("source", {}).get("path")
            if source_path != "./plugins/waggle":
                failures.append(
                    ".agents/plugins/marketplace.json must point waggle to ./plugins/waggle for release bundles"
                )

    failures.extend(_validate_version_consistency(root))
    failures.extend(_validate_plugin_surface(root))

    return failures


def package_release(root: Path, output_dir: Path, bundle_version: str) -> list[Path]:
    failures = validate_bundle_inputs(root)
    if failures:
        raise SystemExit(_format_failures(failures))

    failures.extend(_validate_bundle_version(root, bundle_version))
    if failures:
        raise SystemExit(_format_failures(failures))

    output_dir.mkdir(parents=True, exist_ok=True)

    created_files: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="waggle-codex-plugin-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        created_files.extend(_build_plugin_bundle(root, tmp_root, output_dir, bundle_version))
        created_files.extend(_build_marketplace_bundle(root, tmp_root, output_dir, bundle_version))

    created_files.append(_write_release_manifest(root, output_dir, bundle_version, created_files))
    return created_files


def _build_plugin_bundle(root: Path, tmp_root: Path, output_dir: Path, bundle_version: str) -> list[Path]:
    bundle_root = tmp_root / _bundle_name("plugin", bundle_version)
    _copy_tree(root / PLUGIN_DIR, bundle_root)
    _write_install_notes(bundle_root / "INSTALL.md", marketplace_bundle=False, bundle_version=bundle_version)
    return _write_bundle(bundle_root, output_dir)


def _build_marketplace_bundle(root: Path, tmp_root: Path, output_dir: Path, bundle_version: str) -> list[Path]:
    bundle_root = tmp_root / _bundle_name("marketplace", bundle_version)
    bundle_root.mkdir(parents=True, exist_ok=True)

    for relative_path in ROOT_BUNDLE_FILES:
        source = root / relative_path
        destination = bundle_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    for relative_path in _root_manifest_asset_paths(root):
        source = root / relative_path
        destination = bundle_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    _copy_tree(root / "skills", bundle_root / "skills")
    _copy_tree(root / PLUGIN_DIR, bundle_root / PLUGIN_DIR)
    _write_install_notes(bundle_root / "INSTALL.md", marketplace_bundle=True, bundle_version=bundle_version)
    return _write_bundle(bundle_root, output_dir)


def _copy_tree(source_root: Path, destination_root: Path) -> None:
    destination_root.mkdir(parents=True, exist_ok=True)
    for source_path in sorted(source_root.rglob("*")):
        relative_path = source_path.relative_to(source_root)
        if source_path.is_dir():
            (destination_root / relative_path).mkdir(parents=True, exist_ok=True)
            continue
        if source_path.name == ".gitkeep":
            continue
        destination = destination_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)


def _write_bundle(bundle_root: Path, output_dir: Path) -> list[Path]:
    archive_path = output_dir / f"{bundle_root.name}.zip"
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        for path in sorted(bundle_root.rglob("*")):
            if path.is_dir():
                continue
            relative_path = path.relative_to(bundle_root.parent)
            info = ZipInfo(relative_path.as_posix())
            info.date_time = FIXED_TIMESTAMP
            info.compress_type = ZIP_DEFLATED
            info.external_attr = _zip_mode(path) << 16
            archive.writestr(info, path.read_bytes())

    checksum_path = archive_path.with_suffix(f"{archive_path.suffix}.sha256")
    checksum_path.write_text(f"{_sha256(archive_path)}  {archive_path.name}\n")
    _validate_written_bundle(archive_path)
    return [archive_path, checksum_path]


def _validate_version_consistency(root: Path) -> list[str]:
    failures: list[str] = []
    plugin_paths = [
        root / ".codex-plugin" / "plugin.json",
        root / PLUGIN_DIR / ".codex-plugin" / "plugin.json",
    ]

    plugin_versions: dict[Path, str] = {}
    for plugin_path in plugin_paths:
        if not plugin_path.exists():
            continue
        plugin_version = json.loads(plugin_path.read_text()).get("version")
        if not isinstance(plugin_version, str) or not plugin_version:
            failures.append(f"{plugin_path.relative_to(root).as_posix()} is missing a plugin version")
            continue
        plugin_versions[plugin_path] = plugin_version

    distinct_versions = set(plugin_versions.values())
    if len(distinct_versions) > 1:
        expected_version = next(iter(distinct_versions))
        for plugin_path, plugin_version in plugin_versions.items():
            if plugin_version != expected_version:
                failures.append(
                    f"{plugin_path.relative_to(root).as_posix()} version {plugin_version!r} "
                    f"does not match Codex plugin version {expected_version!r}"
                )

    for plugin_path, plugin_version in plugin_versions.items():
        if _version_tuple(plugin_version) < _version_tuple(MIN_CODEX_PLUGIN_VERSION):
            failures.append(
                f"{plugin_path.relative_to(root).as_posix()} version {plugin_version!r} "
                f"would downgrade the published Codex plugin version {MIN_CODEX_PLUGIN_VERSION!r}"
            )

    return failures


def _validate_plugin_surface(root: Path) -> list[str]:
    failures: list[str] = []
    plugin_root = root / PLUGIN_DIR

    manifest_expectations = {
        root / ".codex-plugin" / "plugin.json": "./skills/",
        plugin_root / ".codex-plugin" / "plugin.json": "./skills/",
    }
    for manifest_path, expected_skills_path in manifest_expectations.items():
        if not manifest_path.exists():
            continue
        manifest = json.loads(manifest_path.read_text())
        display_path = manifest_path.relative_to(root).as_posix()
        if manifest.get("name") != "waggle":
            failures.append(f"{display_path} must declare name 'waggle'")
        if manifest.get("mcpServers") != "./.mcp.json":
            failures.append(f"{display_path} must point mcpServers to ./.mcp.json")
        if manifest.get("skills") != expected_skills_path:
            failures.append(f"{display_path} must point skills to {expected_skills_path}")

    mcp_expectations = {
        root / ".mcp.json": ("./plugins/waggle/bin/waggle-server-launcher.js", "./plugins/waggle"),
        plugin_root / ".mcp.json": ("./bin/waggle-server-launcher.js", "."),
    }
    for mcp_path, (expected_launcher, expected_cwd) in mcp_expectations.items():
        if not mcp_path.exists():
            continue
        payload = json.loads(mcp_path.read_text())
        display_path = mcp_path.relative_to(root).as_posix()
        server_map = payload.get("mcpServers")
        if not isinstance(server_map, dict):
            failures.append(f"{display_path} must contain the Codex-compatible mcpServers map")
            continue
        server = server_map.get("waggle")
        if not isinstance(server, dict):
            failures.append(f"{display_path} is missing the waggle MCP server")
            continue
        if server.get("command") != "node":
            failures.append(f"{display_path} must launch the bundled server through node")
        if server.get("cwd") != expected_cwd:
            failures.append(f"{display_path} must resolve the launcher from plugin cwd {expected_cwd!r}")
        if server.get("args") != [expected_launcher, "serve", "--transport", "stdio"]:
            failures.append(f"{display_path} has an unexpected Waggle startup command")
        env = server.get("env")
        if not isinstance(env, dict) or env.get("WAGGLE_BACKEND") != "sqlite":
            failures.append(f"{display_path} must configure the local SQLite backend")
        if not isinstance(env, dict) or env.get("WAGGLE_TRANSPORT") != "stdio":
            failures.append(f"{display_path} must configure stdio transport")
        if not isinstance(env, dict) or env.get("WAGGLE_MODEL") != "deterministic":
            failures.append(f"{display_path} must configure the offline-safe deterministic model")
        if not isinstance(env, dict) or env.get("WAGGLE_STARTUP_MODE") != "normal":
            failures.append(f"{display_path} must keep memory tools available in normal startup mode")

    for skill_name in CODEX_SKILLS:
        skill_variants: list[str] = []
        for skills_root in (root / "skills", plugin_root / "skills"):
            skill_path = skills_root / skill_name / "SKILL.md"
            if not skill_path.exists():
                failures.append(f"Missing required Codex skill: {skill_path.relative_to(root).as_posix()}")
                continue
            skill_text = skill_path.read_text()
            if not skill_text.startswith("---\n") or f"name: {skill_name}\n" not in skill_text:
                failures.append(f"{skill_path.relative_to(root).as_posix()} has invalid skill frontmatter")
            if "description:" not in skill_text.split("---", 2)[1]:
                failures.append(f"{skill_path.relative_to(root).as_posix()} is missing a skill description")
            skill_variants.append(skill_text)
        if len(skill_variants) == 2 and skill_variants[0] != skill_variants[1]:
            failures.append(f"Root and standalone copies of skill {skill_name!r} must match")

    return failures


def _root_manifest_asset_paths(root: Path) -> list[Path]:
    resolved_root = root.resolve()
    manifest_path = resolved_root / ".codex-plugin" / "plugin.json"
    if not manifest_path.exists():
        return []

    manifest = json.loads(manifest_path.read_text())
    interface = manifest.get("interface", {})
    asset_values: list[str] = []

    for field in ["composerIcon", "logo", "logoDark"]:
        value = interface.get(field)
        if isinstance(value, str):
            asset_values.append(value)

    screenshots = interface.get("screenshots")
    if isinstance(screenshots, list):
        asset_values.extend(value for value in screenshots if isinstance(value, str))

    paths: list[Path] = []
    for value in asset_values:
        if value.startswith("./"):
            source = (resolved_root / value.removeprefix("./")).resolve()
            try:
                paths.append(source.relative_to(resolved_root))
            except ValueError as exc:
                raise ValueError(f"Manifest asset escapes repository root: {value}") from exc
    return sorted(set(paths))


def _validate_bundle_version(root: Path, bundle_version: str) -> list[str]:
    if not bundle_version.startswith("v"):
        return []

    tag_version = bundle_version.removeprefix("v")
    plugin_json_path = root / ".codex-plugin" / "plugin.json"
    if not plugin_json_path.exists():
        return []

    plugin_version = json.loads(plugin_json_path.read_text()).get("version")
    if isinstance(plugin_version, str) and _version_tuple(tag_version) < _version_tuple(plugin_version):
        return [f"release tag {bundle_version!r} is older than Codex plugin version {plugin_version!r}"]
    return []


def _validate_written_bundle(archive_path: Path) -> None:
    if archive_path.stat().st_size > MAX_RELEASE_BUNDLE_BYTES:
        raise SystemExit(
            f"{archive_path.name} is {archive_path.stat().st_size} bytes; limit is {MAX_RELEASE_BUNDLE_BYTES} bytes"
        )

    failures: list[str] = []
    with ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        for target, executable in TARGETS.items():
            if target.startswith("win32-"):
                continue
            matching_names = [name for name in names if name.endswith(f"runtime/{target}/{executable}")]
            if not matching_names:
                failures.append(f"{archive_path.name} is missing executable runtime for {target}")
                continue
            for name in matching_names:
                mode = (archive.getinfo(name).external_attr >> 16) & 0o777
                if not mode & 0o111:
                    failures.append(f"{archive_path.name}:{name} does not preserve executable permissions")

    if failures:
        raise SystemExit(_format_failures(failures))


def _write_release_manifest(root: Path, output_dir: Path, bundle_version: str, files: list[Path]) -> Path:
    artifact_entries = []
    for path in files:
        artifact_entries.append(
            {
                "name": path.name,
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
        )

    manifest_path = output_dir / f"waggle-codex-release-{_sanitize_version(bundle_version)}.json"
    manifest_path.write_text(
        json.dumps(
            {
                "name": "waggle",
                "version": bundle_version,
                "plugin_version": json.loads((root / ".codex-plugin" / "plugin.json").read_text()).get("version"),
                "distribution": MARKETPLACE_DISTRIBUTION,
                "platform_artifact_resolution": "not-supported-by-current-codex-marketplace-schema",
                "artifacts": artifact_entries,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return manifest_path


def _write_install_notes(path: Path, *, marketplace_bundle: bool, bundle_version: str) -> None:
    if marketplace_bundle:
        contents = f"""# Waggle Codex marketplace bundle

This archive contains a complete local Codex marketplace root for the `{bundle_version}` release.
It is self-hosted through GitHub Releases: Codex runs the bundled Waggle stdio
MCP server locally with SQLite storage by default. No paid hosted backend,
Apple Developer ID notarization, or Windows Authenticode certificate is
required for this install path.

The bundled runtime is intentionally unsigned. macOS Gatekeeper or Windows
SmartScreen may show a first-run warning; verify the release checksum or GitHub
attestation before approving the binary.

1. Extract the archive anywhere on disk.
2. Add the extracted directory to Codex:

   codex plugin marketplace add /path/to/{path.parent.name}

3. Install Waggle, then start a new Codex task:

   codex plugin add waggle@waggle

The new task loads the local MCP server and the bundled project-memory skills.
"""
    else:
        contents = f"""# Waggle Codex plugin bundle

This archive contains the bare Waggle plugin folder for the `{bundle_version}` release.
It runs a local stdio MCP server and does not require a hosted Waggle backend or
paid Apple/Windows signing certificates. The current runtime is intentionally
unsigned, so macOS Gatekeeper or Windows SmartScreen may warn on first launch.

For the easiest installation flow, prefer the matching `waggle-codex-marketplace-{bundle_version}.zip`
asset, which includes a ready-to-add local marketplace root for Codex.
"""

    path.write_text(contents)


def _bundle_name(kind: str, bundle_version: str) -> str:
    return f"waggle-codex-{kind}-{_sanitize_version(bundle_version)}"


def _sanitize_version(bundle_version: str) -> str:
    return bundle_version.replace("/", "-").replace("\\", "-").replace(" ", "-")


def _version_tuple(version: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+(?:\.\d+)*)(?:[-+].*)?$", version)
    if not match:
        return (0,)
    return tuple(int(part) for part in match.group(1).split("."))


def _zip_mode(path: Path) -> int:
    parts = path.parts
    if "runtime" in parts:
        runtime_index = parts.index("runtime")
        if len(parts) > runtime_index + 2:
            target = parts[runtime_index + 1]
            executable = parts[runtime_index + 2]
            if target in TARGETS and not target.startswith("win32-") and executable == TARGETS[target]:
                return 0o755

    return 0o755 if path.stat().st_mode & 0o111 else 0o644


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _format_failures(failures: list[str]) -> str:
    return "Codex plugin packaging failed:\n" + "\n".join(f"- {failure}" for failure in failures)


def main() -> int:
    parser = argparse.ArgumentParser(description="Package release-ready Codex plugin bundles for Waggle.")
    parser.add_argument(
        "--bundle-version",
        default="dev",
        help="Version label used in the archive names, for example v0.1.0.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "dist" / "codex-plugin"),
        help="Directory where packaged archives and checksums should be written.",
    )
    parser.add_argument(
        "--root",
        default=str(ROOT),
        help="Repository root containing the Codex plugin and marketplace files.",
    )
    args = parser.parse_args()

    created_files = package_release(Path(args.root), Path(args.output_dir), args.bundle_version)
    for path in created_files:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
