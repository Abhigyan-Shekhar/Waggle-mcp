from __future__ import annotations

import argparse
import copy
import json
import os
import re
import stat
import tempfile
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

MCP_NAME_MARKER = re.compile(r"<!--\s*mcp-name:\s*(?P<name>[^>]+?)\s*-->")
VERSION_SENTINEL = "__WAGGLE_GENERATED_VERSION__"


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


def render_server_json(data: dict[str, Any], *, newline: bytes = b"\n") -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode().replace(b"\n", newline)


def _server_json_newline(original: bytes) -> bytes:
    without_crlf = original.replace(b"\r\n", b"")
    if b"\r\n" in original:
        if b"\n" in without_crlf or b"\r" in without_crlf:
            raise ValueError("server.json has mixed line endings; refusing to rewrite unrelated bytes")
        return b"\r\n"
    if b"\r" in original:
        raise ValueError("server.json has unsupported bare CR line endings; refusing to rewrite unrelated bytes")
    return b"\n"


def set_generated_versions(data: dict[str, Any], version: str) -> None:
    data["version"] = version
    packages = data.get("packages")
    if not isinstance(packages, list):
        raise ValueError("server.json .packages must be an array")
    if not packages:
        raise ValueError("server.json .packages must declare at least one package")
    for index, package in enumerate(packages):
        if not isinstance(package, dict):
            raise ValueError(f"server.json .packages[{index}] must be an object")
        package["version"] = version


def _changed_line_indexes(before: bytes, after: bytes) -> set[int]:
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    if len(before_lines) != len(after_lines):
        raise ValueError("server.json rewrite would change the number of lines")
    return {
        index
        for index, (old_line, new_line) in enumerate(zip(before_lines, after_lines, strict=True))
        if old_line != new_line
    }


def build_server_candidate(original: bytes, data: dict[str, Any], version: str) -> bytes:
    newline = _server_json_newline(original)
    canonical_original = render_server_json(data, newline=newline)
    if canonical_original != original:
        raise ValueError("server.json is not in canonical two-space JSON format; refusing to rewrite unrelated bytes")

    sentinel_data = copy.deepcopy(data)
    set_generated_versions(sentinel_data, VERSION_SENTINEL)
    allowed_lines = _changed_line_indexes(
        canonical_original,
        render_server_json(sentinel_data, newline=newline),
    )

    candidate_data = copy.deepcopy(data)
    set_generated_versions(candidate_data, version)
    candidate = render_server_json(candidate_data, newline=newline)
    changed_lines = _changed_line_indexes(original, candidate)
    unexpected_lines = changed_lines - allowed_lines
    if unexpected_lines:
        display_lines = ", ".join(str(index + 1) for index in sorted(unexpected_lines))
        raise ValueError(f"server.json rewrite changed unrelated lines: {display_lines}")
    return candidate


def _stage_bytes(target: Path, data: bytes) -> Path:
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    staged = Path(name)
    try:
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written == 0:
                    raise OSError(f"failed to stage {target.name}: write returned zero bytes")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        staged.chmod(stat.S_IMODE(target.stat().st_mode))
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


def _replace_files(changes: Sequence[tuple[Path, bytes, bytes]]) -> None:
    candidates: list[tuple[Path, Path]] = []
    originals: list[tuple[Path, Path]] = []
    replaced: list[tuple[Path, Path]] = []
    try:
        for target, original_bytes, candidate_bytes in changes:
            candidates.append((target, _stage_bytes(target, candidate_bytes)))
            originals.append((target, _stage_bytes(target, original_bytes)))

        try:
            for (target, candidate_path), (_, original_path) in zip(candidates, originals, strict=True):
                os.replace(candidate_path, target)
                replaced.append((target, original_path))
        except BaseException as replace_error:
            rollback_errors: list[str] = []
            for target, original_path in reversed(replaced):
                try:
                    os.replace(original_path, target)
                except OSError as rollback_error:
                    rollback_errors.append(f"{target.name}: {rollback_error}")
            if rollback_errors:
                details = "; ".join(rollback_errors)
                raise OSError(f"{replace_error}; rollback failed for {details}") from replace_error
            raise
    finally:
        for _, staged in candidates + originals:
            staged.unlink(missing_ok=True)


def write_metadata(root: Path | None = None) -> list[str]:
    repo_root = resolve_root(root)
    try:
        project_version = load_project_version(repo_root)
        server_path = repo_root / "server.json"
        readme_path = repo_root / "README.md"
        server_original = server_path.read_bytes()
        server = load_server_metadata(repo_root)
        server_candidate = build_server_candidate(server_original, server, project_version)

        name = server.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("server.json .name must be a non-empty string")
        readme_original = readme_path.read_bytes()
        readme_text = readme_original.decode()
        markers = find_readme_markers(readme_text)
        readme_candidate = readme_original
        if not markers:
            readme_newline = b"\r\n" if b"\r\n" in readme_original else b"\n"
            readme_candidate = f"<!-- mcp-name: {name} -->".encode() + readme_newline + readme_newline + readme_original

        changes = []
        if server_candidate != server_original:
            changes.append((server_path, server_original, server_candidate))
        if readme_candidate != readme_original:
            changes.append((readme_path, readme_original, readme_candidate))
        _replace_files(changes)
        return check_metadata(repo_root)
    except (OSError, UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
        return [str(exc)]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check or synchronize release metadata.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="report drift without writing")
    mode.add_argument("--write", action="store_true", help="synchronize generated metadata")
    return parser


def main(argv: Sequence[str] | None = None, *, root: Path | None = None) -> int:
    args = _parser().parse_args(argv)
    issues = write_metadata(root) if args.write else check_metadata(root)
    if issues:
        print("Release metadata validation failed:")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print("Release metadata is consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
