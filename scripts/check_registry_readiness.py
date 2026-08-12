from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import tomllib
from collections.abc import Sequence
from email.parser import Parser
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

import jsonschema

MCP_NAME_MARKER = re.compile(r"<!--\s*mcp-name:\s*(?P<name>[^>]+?)\s*-->")


def _exception_summary(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return " | ".join(_exception_summary(nested) for nested in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} root must be an object")
    return payload


def _schema_error_path(error: jsonschema.ValidationError) -> str:
    parts: list[str] = []
    for item in error.absolute_path:
        if isinstance(item, int):
            parts[-1] = f"{parts[-1]}[{item}]"
        else:
            parts.append(str(item))
    return ".".join(parts) or "root"


def check_schema(manifest_path: Path, schema_path: Path) -> list[str]:
    try:
        manifest = _read_json_object(manifest_path)
        schema = _read_json_object(schema_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return [f"schema inputs could not be loaded: {exc}"]

    declared_schema = manifest.get("$schema")
    schema_id = schema.get("$id")
    if declared_schema != schema_id:
        return [f"server.json $schema {declared_schema!r} does not match registry schema $id {schema_id!r}"]

    try:
        validator_type = jsonschema.validators.validator_for(schema)
        validator_type.check_schema(schema)
    except jsonschema.SchemaError as exc:
        return [f"registry schema is invalid: {exc.message}"]

    return [
        f"server.json {_schema_error_path(error)}: {error.message}"
        for error in sorted(validator_type(schema).iter_errors(manifest), key=lambda item: list(item.absolute_path))
    ]


def check_project_metadata(root: Path) -> list[str]:
    try:
        manifest = _read_json_object(root / "server.json")
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
        return [f"project metadata could not be loaded: {exc}"]

    project = pyproject.get("project")
    if not isinstance(project, dict):
        return ["pyproject.toml must declare [project]"]
    project_name = project.get("name")
    if not isinstance(project_name, str) or not project_name:
        return ["pyproject.toml [project].name must be a non-empty string"]

    issues: list[str] = []
    packages = manifest.get("packages")
    if not isinstance(packages, list):
        return ["server.json packages must be an array"]
    pypi_packages = [
        package for package in packages if isinstance(package, dict) and package.get("registryType") == "pypi"
    ]
    if not pypi_packages:
        issues.append("server.json must declare a PyPI package")
    for package in pypi_packages:
        identifier = package.get("identifier")
        if identifier != project_name:
            issues.append(
                f"server.json PyPI package {identifier!r} does not match pyproject.toml [project].name {project_name!r}"
            )

    scripts = project.get("scripts")
    if not isinstance(scripts, dict) or not isinstance(scripts.get("waggle-mcp"), str):
        issues.append("pyproject.toml [project.scripts] must declare waggle-mcp")
    return issues


def check_manifest(root: Path, schema_path: Path) -> list[str]:
    schema_issues = check_schema(root / "server.json", schema_path)
    return [*schema_issues, *check_project_metadata(root)]


def _normalized_description(value: str) -> str:
    return value.replace("\r\n", "\n").rstrip("\n")


def check_wheel(wheel_path: Path, readme_path: Path) -> list[str]:
    try:
        readme = readme_path.read_text(encoding="utf-8")
        markers = MCP_NAME_MARKER.findall(readme)
        if len(markers) != 1:
            return [f"README.md must contain exactly one mcp-name marker; found {len(markers)}"]
        marker = f"<!-- mcp-name: {markers[0].strip()} -->"
        with ZipFile(wheel_path) as archive:
            metadata_members = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata_members) != 1:
                return [f"wheel must contain exactly one .dist-info/METADATA file; found {len(metadata_members)}"]
            metadata_text = archive.read(metadata_members[0]).decode("utf-8")
    except (OSError, UnicodeError, BadZipFile) as exc:
        return [f"wheel metadata could not be loaded: {exc}"]

    description = Parser().parsestr(metadata_text).get_payload()
    if not isinstance(description, str):
        return ["wheel METADATA description must be plain text"]
    issues: list[str] = []
    if _normalized_description(description) != _normalized_description(readme):
        issues.append("wheel METADATA description does not exactly match README.md")
    if marker not in description:
        issues.append(f"wheel METADATA description is missing {marker}")
    return issues


async def _run_stdio_smoke(command: Path, work_dir: Path) -> list[str]:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    if not command.is_file():
        return [f"installed command does not exist: {command}"]
    work_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "WAGGLE_BACKEND": "sqlite",
            "WAGGLE_BANNER": "false",
            "WAGGLE_DB_PATH": str(work_dir / "stdio-smoke.db"),
            "WAGGLE_DEFAULT_TENANT_ID": "registry-smoke",
            "WAGGLE_MODEL": "deterministic",
            "WAGGLE_STARTUP_MODE": "fast",
            "WAGGLE_TRANSPORT": "stdio",
        }
    )
    server = StdioServerParameters(
        command=str(command),
        args=["serve", "--transport", "stdio", "--quiet"],
        cwd=str(work_dir),
        env=env,
    )
    try:
        async with (
            stdio_client(server) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            if initialized.server_info.name != "waggle":
                return [f"stdio server name was {initialized.server_info.name!r}, expected 'waggle'"]
            tools = await session.list_tools()
            if "store_node" not in {tool.name for tool in tools.tools}:
                return ["stdio server did not advertise the store_node tool"]
    except Exception as exc:
        return [f"stdio protocol smoke test failed: {_exception_summary(exc)}"]
    return []


async def smoke_stdio(command: Path, work_dir: Path) -> list[str]:
    try:
        return await asyncio.wait_for(_run_stdio_smoke(command, work_dir), timeout=30.0)
    except TimeoutError:
        return ["stdio protocol smoke test timed out after 30 seconds"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate MCP Registry release readiness.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    schema = subparsers.add_parser("schema", help="validate server.json against a downloaded registry schema")
    schema.add_argument("--manifest", type=Path, default=Path("server.json"))
    schema.add_argument("--schema", type=Path, required=True)

    project = subparsers.add_parser("project", help="validate registry package and Python entry-point metadata")
    project.add_argument("--root", type=Path, default=Path.cwd())

    manifest = subparsers.add_parser("manifest", help="run both schema and project metadata validation")
    manifest.add_argument("--root", type=Path, default=Path.cwd())
    manifest.add_argument("--schema", type=Path, required=True)

    wheel = subparsers.add_parser("wheel", help="validate the built wheel's embedded README")
    wheel.add_argument("--wheel", type=Path, required=True)
    wheel.add_argument("--readme", type=Path, default=Path("README.md"))

    stdio = subparsers.add_parser("stdio", help="smoke-test an installed waggle-mcp command over stdio")
    stdio.add_argument("--command", type=Path, required=True)
    stdio.add_argument("--work-dir", type=Path, required=True)
    return parser


def _print_result(issues: list[str]) -> int:
    if issues:
        print("Registry readiness validation failed:")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print("Registry readiness validation passed.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "schema":
        return _print_result(check_schema(args.manifest, args.schema))
    if args.command == "project":
        return _print_result(check_project_metadata(args.root))
    if args.command == "manifest":
        return _print_result(check_manifest(args.root, args.schema))
    if args.command == "wheel":
        return _print_result(check_wheel(args.wheel, args.readme))
    return _print_result(asyncio.run(smoke_stdio(args.command, args.work_dir)))


if __name__ == "__main__":
    raise SystemExit(main())
