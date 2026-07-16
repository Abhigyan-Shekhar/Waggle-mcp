#!/usr/bin/env python3
"""
check_extension_metadata.py -- Claude Desktop Extension Metadata Validator.

Verifies that versioning-critical fields in the packaging metadata (package.json)
and the extension manifest (manifest.json) remain aligned to prevent silent
version drift during packaging and releases.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MANIFEST = REPO_ROOT / "apps" / "mcp" / "claude-desktop-extension" / "manifest.json"
DEFAULT_PACKAGE = REPO_ROOT / "apps" / "mcp" / "claude-desktop-extension" / "package.json"


def check_metadata(manifest_path: Path, package_path: Path, verbose: bool = False) -> int:
    """Compare version and critical fields between manifest.json and package.json.

    Returns:
        0 if they are fully aligned.
        1 if there is a mismatch.
        2 if files cannot be read or parsed.
    """
    if not manifest_path.exists():
        print(f"ERROR: Manifest file not found: {manifest_path}", file=sys.stderr)
        return 2
    if not package_path.exists():
        print(f"ERROR: Package file not found: {package_path}", file=sys.stderr)
        return 2

    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"ERROR: Failed to parse manifest.json: {exc}", file=sys.stderr)
        return 2

    if not isinstance(manifest_data, dict):
        print("ERROR: manifest.json root must be a JSON object", file=sys.stderr)
        return 2

    try:
        package_data = json.loads(package_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"ERROR: Failed to parse package.json: {exc}", file=sys.stderr)
        return 2

    if not isinstance(package_data, dict):
        print("ERROR: package.json root must be a JSON object", file=sys.stderr)
        return 2

    manifest_version = manifest_data.get("version")
    package_version = package_data.get("version")

    if verbose:
        print(f"Loaded manifest version: {manifest_version}")
        print(f"Loaded package version:  {package_version}")

    if not manifest_version:
        print("ERROR: 'version' field is missing or empty in manifest.json", file=sys.stderr)
        return 1
    if not package_version:
        print("ERROR: 'version' field is missing or empty in package.json", file=sys.stderr)
        return 1

    if manifest_version != package_version:
        print("[FAIL] Metadata version drift detected!", file=sys.stderr)
        print(f"  manifest.json version: {manifest_version}", file=sys.stderr)
        print(f"  package.json version:  {package_version}", file=sys.stderr)
        print("\nHow to fix:", file=sys.stderr)
        print("  Align the 'version' field in both files to be identical:", file=sys.stderr)
        print(f"  - {manifest_path}", file=sys.stderr)
        print(f"  - {package_path}", file=sys.stderr)
        return 1

    print("[OK] Metadata versions are aligned.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check version alignment between Claude Desktop extension manifest and package metadata."
    )
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST),
        help="Path to manifest.json (default: apps/mcp/claude-desktop-extension/manifest.json)",
    )
    parser.add_argument(
        "--package",
        default=str(DEFAULT_PACKAGE),
        help="Path to package.json (default: apps/mcp/claude-desktop-extension/package.json)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed version information",
    )
    args = parser.parse_args(argv)

    return check_metadata(Path(args.manifest), Path(args.package), args.verbose)


if __name__ == "__main__":
    sys.exit(main())
