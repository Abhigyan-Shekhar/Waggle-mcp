#!/usr/bin/env python3
"""
Sync or validate version numbers across python, VS Code, and Claude extension manifests.
"""

import argparse
import contextlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Paths relative to repository root
FILES = {
    "pyproject.toml": "toml",
    "apps/vscode-extension/package.json": "json",
    "apps/vscode-extension/package-lock.json": "json_lock",
    "apps/mcp/claude-desktop-extension/package.json": "json",
    "apps/mcp/claude-desktop-extension/package-lock.json": "json_lock",
    "apps/mcp/claude-desktop-extension/manifest.json": "json",
}


def read_toml_version(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', content)
    if not match:
        raise ValueError(f"Could not find version in {path}")
    return match.group(1)


def get_toml_new_content(path: Path, version: str) -> str:
    content = path.read_text(encoding="utf-8")
    new_content, count = re.subn(r'(?m)^version\s*=\s*"[^"]+"', f'version = "{version}"', content)
    if count == 0:
        raise ValueError(f"Could not find version line to replace in {path}")
    return new_content


def read_json_version(path: Path) -> str:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["version"]


def get_json_new_content(path: Path, version: str) -> str:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["version"] = version
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def read_json_lock_version(path: Path) -> str:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    root_version = data.get("version")
    packages = data.get("packages", {})
    pkg_version = packages.get("", {}).get("version") if isinstance(packages, dict) else None

    if root_version != pkg_version:
        raise ValueError(
            f"Internal version mismatch in {path}: root version is '{root_version}' but packages[''] version is '{pkg_version}'"
        )
    return root_version


def get_json_lock_new_content(path: Path, version: str) -> str:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["version"] = version
    if "packages" in data and "" in data["packages"]:
        data["packages"][""]["version"] = version
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def read_version(rel_path: str, file_type: str) -> str:
    path = REPO_ROOT / rel_path
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if file_type == "toml":
        return read_toml_version(path)
    elif file_type == "json":
        return read_json_version(path)
    elif file_type == "json_lock":
        return read_json_lock_version(path)
    else:
        raise ValueError(f"Unknown file type: {file_type}")


def prepare_new_content(rel_path: str, file_type: str, version: str) -> str:
    path = REPO_ROOT / rel_path
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if file_type == "toml":
        return get_toml_new_content(path, version)
    elif file_type == "json":
        return get_json_new_content(path, version)
    elif file_type == "json_lock":
        return get_json_lock_new_content(path, version)
    else:
        raise ValueError(f"Unknown file type: {file_type}")


def check_versions() -> bool:
    versions = {}
    has_error = False

    for rel_path, file_type in FILES.items():
        try:
            version = read_version(rel_path, file_type)
            versions[rel_path] = version
        except Exception as e:
            print(f"Error reading {rel_path}: {e}")
            has_error = True

    if has_error:
        return False

    unique_versions = set(versions.values())
    if len(unique_versions) > 1:
        print("Mismatched versions found:")
        for rel_path, version in versions.items():
            print(f"  {rel_path}: {version}")
        return False

    print(f"All files are in sync at version: {next(iter(unique_versions))}")
    return True


def sync_versions(target_version: str | None = None) -> bool:
    if target_version is None:
        try:
            target_version = read_version("pyproject.toml", "toml")
            print(f"Read single source of truth version '{target_version}' from pyproject.toml")
        except Exception as e:
            print(f"Error reading pyproject.toml: {e}")
            return False

    # Prepare all new contents in memory first to prevent partial/non-atomic updates
    prepared_updates = {}
    for rel_path, file_type in FILES.items():
        try:
            new_content = prepare_new_content(rel_path, file_type, target_version)
            prepared_updates[rel_path] = new_content
        except Exception as e:
            print(f"Error preparing version update for {rel_path}: {e}")
            return False

    # Atomic write-out with rollback capability
    print(f"Syncing all files to version: {target_version}")

    # 1. Read and keep all original contents of FILES in memory for rollback
    original_contents = {}
    for rel_path in prepared_updates:
        path = REPO_ROOT / rel_path
        try:
            original_contents[rel_path] = path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"Error reading original file {rel_path} for backup: {e}")
            return False

    # 2. Write new content to temporary files in the same directory
    temp_files = []
    try:
        for rel_path, new_content in prepared_updates.items():
            path = REPO_ROOT / rel_path
            temp_path = path.with_name(path.name + f".tmp-{target_version}")
            temp_path.write_text(new_content, encoding="utf-8")
            temp_files.append((rel_path, temp_path))
    except Exception as e:
        print(f"Error writing temporary files: {e}. Aborting update.")
        # Clean up temporary files
        for _, temp_path in temp_files:
            with contextlib.suppress(Exception):
                temp_path.unlink(missing_ok=True)
        return False

    # 3. Replace target files with temporary files (atomic replacement)
    replaced_files = []
    try:
        for rel_path, temp_path in temp_files:
            path = REPO_ROOT / rel_path
            temp_path.replace(path)
            replaced_files.append(rel_path)
            print(f"  Updated {rel_path}")
    except Exception as e:
        print(f"Error replacing files: {e}. Rolling back updates...")
        # Revert all successfully replaced files from backups
        for rel_path in replaced_files:
            try:
                path = REPO_ROOT / rel_path
                path.write_text(original_contents[rel_path], encoding="utf-8")
                print(f"  Rolled back {rel_path}")
            except Exception as rollback_err:
                print(f"  Failed to roll back {rel_path}: {rollback_err}")
        # Clean up remaining temp files
        for _, temp_path in temp_files:
            with contextlib.suppress(Exception):
                temp_path.unlink(missing_ok=True)
        return False

    return True


def main():
    parser = argparse.ArgumentParser(description="Sync or check versions across configuration manifests.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Check that all manifest versions are identical.")
    group.add_argument(
        "--sync",
        nargs="?",
        const="",
        help="Sync versions. If a version is provided, syncs to that. If empty, syncs other files to pyproject.toml version.",
    )

    args = parser.parse_args()

    if args.check:
        if not check_versions():
            sys.exit(1)
    elif args.sync is not None:
        target = args.sync if args.sync != "" else None
        if not sync_versions(target):
            sys.exit(1)


if __name__ == "__main__":
    main()
