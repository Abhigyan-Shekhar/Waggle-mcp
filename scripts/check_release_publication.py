from __future__ import annotations

import argparse
import json
import sys
import time
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen

REQUEST_TIMEOUT_SECONDS = 10.0
PYPI_ATTEMPTS = 20
PYPI_INTERVAL_SECONDS = 15.0
REGISTRY_ATTEMPTS = 12
REGISTRY_INTERVAL_SECONDS = 10.0


class ReleaseCheckError(RuntimeError):
    """A deterministic release verification failure."""


JsonFetcher = Callable[[str, float], tuple[int, object]]
Sleeper = Callable[[float], None]


def load_project_version(root: Path) -> str:
    try:
        payload = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseCheckError(f"could not load pyproject.toml: {exc}") from exc
    project = payload.get("project")
    if not isinstance(project, dict):
        raise ReleaseCheckError("pyproject.toml must declare a [project] table")
    version = project.get("version")
    if not isinstance(version, str) or not version:
        raise ReleaseCheckError("pyproject.toml [project].version must be a non-empty string")
    return version


def validate_tag(root: Path, tag: str) -> str:
    version = load_project_version(root)
    expected = f"v{version}"
    if tag != expected:
        raise ReleaseCheckError(f"tag {tag} does not match project version {version}; expected {expected}")
    return version


def fetch_json(url: str, timeout: float) -> tuple[int, object]:
    try:
        with urlopen(url, timeout=timeout) as response:
            status = response.status
            body = response.read()
    except HTTPError as exc:
        if exc.code == 404:
            return 404, {}
        raise ReleaseCheckError(f"request to {url} returned HTTP {exc.code}") from exc
    except (TimeoutError, URLError, OSError) as exc:
        raise ReleaseCheckError(f"request to {url} failed: {exc}") from exc

    try:
        return status, json.loads(body)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ReleaseCheckError(f"response was not valid JSON for {url}: {exc}") from exc


def _require_object(payload: object, key: str, source: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReleaseCheckError(f"{source} response root must be an object")
    value = payload.get(key)
    if not isinstance(value, dict):
        article = "an" if key[0].lower() in "aeiou" else "a"
        raise ReleaseCheckError(f"{source} response must contain {article} {key} object")
    return value


def _poll(
    *,
    url: str,
    source: str,
    not_visible_message: str,
    validate: Callable[[object], None],
    fetcher: JsonFetcher,
    sleeper: Sleeper,
    attempts: int,
    interval: float,
) -> None:
    if attempts < 1:
        raise ReleaseCheckError(f"{source} polling attempts must be at least 1")
    for attempt in range(attempts):
        status, payload = fetcher(url, REQUEST_TIMEOUT_SECONDS)
        if status == 404:
            if attempt + 1 < attempts:
                sleeper(interval)
            continue
        if status != 200:
            raise ReleaseCheckError(f"{source} request returned HTTP {status}")
        validate(payload)
        return
    raise ReleaseCheckError(not_visible_message)


def poll_pypi(
    project: str,
    version: str,
    *,
    fetch_json: JsonFetcher = fetch_json,
    sleep: Sleeper = time.sleep,
    attempts: int = PYPI_ATTEMPTS,
    interval: float = PYPI_INTERVAL_SECONDS,
) -> None:
    project_path = quote(project, safe="")
    version_path = quote(version, safe="")
    url = f"https://pypi.org/pypi/{project_path}/{version_path}/json"

    def validate(payload: object) -> None:
        info = _require_object(payload, "info", "PyPI")
        actual_name = info.get("name")
        if actual_name != project:
            raise ReleaseCheckError(f"PyPI project name {actual_name!r} does not match expected {project!r}")
        actual_version = info.get("version")
        if actual_version != version:
            raise ReleaseCheckError(f"PyPI project version {actual_version!r} does not match expected {version!r}")

    _poll(
        url=url,
        source="PyPI",
        not_visible_message=f"PyPI version {project} {version} was not visible after {attempts} attempts",
        validate=validate,
        fetcher=fetch_json,
        sleeper=sleep,
        attempts=attempts,
        interval=interval,
    )


def poll_registry(
    name: str,
    version: str,
    package: str,
    *,
    fetch_json: JsonFetcher = fetch_json,
    sleep: Sleeper = time.sleep,
    attempts: int = REGISTRY_ATTEMPTS,
    interval: float = REGISTRY_INTERVAL_SECONDS,
) -> None:
    name_path = quote(name, safe="")
    version_path = quote(version, safe="")
    url = f"https://registry.modelcontextprotocol.io/v0/servers/{name_path}/versions/{version_path}"

    def validate(payload: object) -> None:
        server = _require_object(payload, "server", "Registry")
        actual_name = server.get("name")
        if actual_name != name:
            raise ReleaseCheckError(f"Registry server name {actual_name!r} does not match expected {name!r}")
        actual_version = server.get("version")
        if actual_version != version:
            raise ReleaseCheckError(f"Registry server version {actual_version!r} does not match expected {version!r}")
        packages = server.get("packages")
        if not isinstance(packages, list):
            raise ReleaseCheckError("Registry server packages must be an array")
        pypi_packages = [item for item in packages if isinstance(item, dict) and item.get("registryType") == "pypi"]
        if len(pypi_packages) != 1:
            raise ReleaseCheckError(
                f"Registry response must contain exactly one PyPI package; found {len(pypi_packages)}"
            )
        pypi_package = pypi_packages[0]
        identifier = pypi_package.get("identifier")
        if identifier != package:
            raise ReleaseCheckError(
                f"Registry PyPI package identifier {identifier!r} does not match expected {package!r}"
            )
        package_version = pypi_package.get("version")
        if package_version != version:
            raise ReleaseCheckError(
                f"Registry PyPI package version {package_version!r} does not match expected {version!r}"
            )

    _poll(
        url=url,
        source="MCP Registry",
        not_visible_message=f"MCP Registry version {name} {version} was not visible after {attempts} attempts",
        validate=validate,
        fetcher=fetch_json,
        sleeper=sleep,
        attempts=attempts,
        interval=interval,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify release tags and public publication metadata.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    tag = subparsers.add_parser("tag", help="require a tag matching the project version")
    tag.add_argument("--tag", required=True)

    pypi = subparsers.add_parser("pypi", help="wait for an exact PyPI release")
    pypi.add_argument("--project", required=True)
    pypi.add_argument("--version", required=True)

    registry = subparsers.add_parser("registry", help="wait for an exact MCP Registry release")
    registry.add_argument("--name", required=True)
    registry.add_argument("--version", required=True)
    registry.add_argument("--package", required=True)
    return parser


def main(argv: Sequence[str] | None = None, *, root: Path | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = root if root is not None else Path(__file__).resolve().parents[1]
    try:
        if args.command == "tag":
            print(validate_tag(repo_root, args.tag))
        elif args.command == "pypi":
            poll_pypi(args.project, args.version)
        else:
            poll_registry(args.name, args.version, args.package)
    except ReleaseCheckError as exc:
        print(f"Release publication check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
