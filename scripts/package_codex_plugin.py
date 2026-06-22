from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from scripts.build_codex_plugin_runtime import TARGETS

ROOT = Path(__file__).resolve().parents[1]
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

    return failures


def package_release(root: Path, output_dir: Path, bundle_version: str) -> list[Path]:
    failures = validate_bundle_inputs(root)
    if failures:
        raise SystemExit(_format_failures(failures))

    output_dir.mkdir(parents=True, exist_ok=True)

    created_files: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="waggle-codex-plugin-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        created_files.extend(_build_plugin_bundle(root, tmp_root, output_dir, bundle_version))
        created_files.extend(_build_marketplace_bundle(root, tmp_root, output_dir, bundle_version))

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
            info.external_attr = (_zip_mode(path) << 16)
            archive.writestr(info, path.read_bytes())

    checksum_path = archive_path.with_suffix(f"{archive_path.suffix}.sha256")
    checksum_path.write_text(f"{_sha256(archive_path)}  {archive_path.name}\n")
    return [archive_path, checksum_path]


def _write_install_notes(path: Path, *, marketplace_bundle: bool, bundle_version: str) -> None:
    if marketplace_bundle:
        contents = f"""# Waggle Codex marketplace bundle

This archive contains a complete local Codex marketplace root for the `{bundle_version}` release.

1. Extract the archive anywhere on disk.
2. Add the extracted directory to Codex:

   codex plugin marketplace add /path/to/{path.parent.name}

3. Refresh the plugin directory in Codex and install `Waggle` from that marketplace.
"""
    else:
        contents = f"""# Waggle Codex plugin bundle

This archive contains the bare Waggle plugin folder for the `{bundle_version}` release.

For the easiest installation flow, prefer the matching `waggle-codex-marketplace-{bundle_version}.zip`
asset, which includes a ready-to-add local marketplace root for Codex.
"""

    path.write_text(contents)


def _bundle_name(kind: str, bundle_version: str) -> str:
    sanitized_version = bundle_version.replace("/", "-").replace("\\", "-").replace(" ", "-")
    return f"waggle-codex-{kind}-{sanitized_version}"


def _zip_mode(path: Path) -> int:
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
