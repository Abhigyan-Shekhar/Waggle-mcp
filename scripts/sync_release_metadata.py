from __future__ import annotations

import argparse
import json
import re
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

MCP_NAME_MARKER = re.compile(r"<!--\s*mcp-name:\s*(?P<name>[^>]+?)\s*-->")


def resolve_root(root: Path | None) -> Path:
    return root if root is not None else Path(__file__).resolve().parents[1]


def load_project_version(root: Path) -> str:
    payload = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = payload.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("pyproject.toml [project].version must be a non-empty string")
    return version


def load_server_metadata(root: Path) -> dict[str, Any]:
    payload = json.loads((root / "server.json").read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("server.json root must be an object")
    return payload


def find_readme_markers(readme: str) -> list[str]:
    return [match.group("name").strip() for match in MCP_NAME_MARKER.finditer(readme)]


def _semantic_issues(root: Path) -> list[str]:
    project_version = load_project_version(root)
    server = load_server_metadata(root)
    issues: list[str] = []

    server_version = server.get("version")
    if server_version != project_version:
        issues.append(f"server.json .version: expected {project_version}, found {server_version}")

    packages = server.get("packages")
    if not isinstance(packages, list):
        raise ValueError("server.json .packages must be an array")
    if not packages:
        issues.append("server.json .packages must declare at least one package")
    for index, package in enumerate(packages):
        if not isinstance(package, dict):
            raise ValueError(f"server.json .packages[{index}] must be an object")
        package_version = package.get("version")
        if package_version != project_version:
            issues.append(
                f"server.json .packages[{index}].version: expected {project_version}, found {package_version}"
            )

    name = server.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("server.json .name must be a non-empty string")
    markers = find_readme_markers((root / "README.md").read_text(encoding="utf-8"))
    if not markers:
        issues.append(f"README.md: missing <!-- mcp-name: {name} -->")
    elif len(markers) > 1:
        issues.append(f"README.md: found {len(markers)} mcp-name markers; expected exactly one")
    elif markers[0] != name:
        issues.append(f"README.md mcp-name marker {markers[0]!r} does not match server.json .name {name!r}")
    return issues


def check_metadata(root: Path | None = None) -> list[str]:
    repo_root = resolve_root(root)
    try:
        return _semantic_issues(repo_root)
    except (OSError, UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
        return [f"metadata validation failed: {exc}"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check release metadata.")
    parser.add_argument("--check", action="store_true", required=True, help="report drift without writing")
    return parser


def main(argv: Sequence[str] | None = None, *, root: Path | None = None) -> int:
    _parser().parse_args(argv)
    issues = check_metadata(root)
    if issues:
        print("Release metadata validation failed:")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print("Release metadata is consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
