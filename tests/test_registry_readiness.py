from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from scripts.check_registry_readiness import (
    check_manifest,
    check_project_metadata,
    check_schema,
    check_wheel,
    smoke_stdio,
)

NAME = "io.github.Abhigyan-Shekhar/Waggle-mcp"
MARKER = f"<!-- mcp-name: {NAME} -->"


def write_repository_fixture(root: Path) -> Path:
    (root / "README.md").write_text(f"{MARKER}\n\n# Waggle\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        """[project]
name = "waggle-mcp"
version = "0.1.22"

[project.scripts]
waggle-mcp = "waggle.server:main"
""",
        encoding="utf-8",
    )
    (root / "server.json").write_text(
        json.dumps(
            {
                "$schema": "https://example.invalid/server.schema.json",
                "name": NAME,
                "title": "Waggle",
                "description": "Persistent, local-first, graph-backed conversational memory.",
                "version": "0.1.22",
                "packages": [
                    {
                        "registryType": "pypi",
                        "identifier": "waggle-mcp",
                        "version": "0.1.22",
                        "transport": {"type": "stdio"},
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    schema_path = root / "server.schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "https://example.invalid/server.schema.json",
                "type": "object",
                "required": ["$schema", "name", "title", "packages"],
                "properties": {
                    "title": {"const": "Waggle"},
                    "packages": {"type": "array", "minItems": 1},
                },
            }
        ),
        encoding="utf-8",
    )
    return schema_path


def write_wheel_fixture(path: Path, readme: str) -> None:
    metadata = (
        f"Metadata-Version: 2.4\nName: waggle-mcp\nVersion: 0.1.22\nDescription-Content-Type: text/markdown\n\n{readme}"
    )
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("waggle_mcp-0.1.22.dist-info/METADATA", metadata)


def test_manifest_check_accepts_schema_valid_consistent_metadata(tmp_path: Path) -> None:
    schema_path = write_repository_fixture(tmp_path)

    assert check_manifest(tmp_path, schema_path) == []


def test_schema_and_project_checks_can_run_as_separate_ordered_ci_steps(tmp_path: Path) -> None:
    schema_path = write_repository_fixture(tmp_path)

    assert check_schema(tmp_path / "server.json", schema_path) == []
    assert check_project_metadata(tmp_path) == []


def test_schema_cli_does_not_require_mcp_sdk(tmp_path: Path) -> None:
    schema_path = write_repository_fixture(tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts" / "check_registry_readiness.py"
    code = (
        "import runpy, sys; "
        "sys.modules['mcp'] = None; "
        f"sys.argv = [{str(script)!r}, 'schema', '--manifest', {str(tmp_path / 'server.json')!r}, "
        f"'--schema', {str(schema_path)!r}]; "
        f"runpy.run_path({str(script)!r}, run_name='__main__')"
    )

    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stderr
    assert "Registry readiness validation passed." in completed.stdout


def test_manifest_check_reports_schema_validation_path(tmp_path: Path) -> None:
    schema_path = write_repository_fixture(tmp_path)
    manifest_path = tmp_path / "server.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["title"] = "Not Waggle"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert check_manifest(tmp_path, schema_path) == ["server.json title: 'Waggle' was expected"]


def test_schema_check_rejects_downloaded_schema_with_wrong_identity(tmp_path: Path) -> None:
    schema_path = write_repository_fixture(tmp_path)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["$id"] = "https://example.invalid/different.schema.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")

    assert check_schema(tmp_path / "server.json", schema_path) == [
        "server.json $schema 'https://example.invalid/server.schema.json' does not match "
        "registry schema $id 'https://example.invalid/different.schema.json'"
    ]


def test_manifest_check_reports_pypi_project_name_drift(tmp_path: Path) -> None:
    schema_path = write_repository_fixture(tmp_path)
    manifest_path = tmp_path / "server.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["packages"][0]["identifier"] = "different-package"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert check_manifest(tmp_path, schema_path) == [
        "server.json PyPI package 'different-package' does not match pyproject.toml [project].name 'waggle-mcp'"
    ]


def test_manifest_check_requires_declared_waggle_mcp_entry_point(tmp_path: Path) -> None:
    schema_path = write_repository_fixture(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "waggle-mcp"\nversion = "0.1.22"\n',
        encoding="utf-8",
    )

    assert check_manifest(tmp_path, schema_path) == ["pyproject.toml [project.scripts] must declare waggle-mcp"]


def test_wheel_check_accepts_exact_embedded_readme_and_marker(tmp_path: Path) -> None:
    readme_path = tmp_path / "README.md"
    readme = f"{MARKER}\n\n# Waggle\n"
    readme_path.write_text(readme, encoding="utf-8")
    wheel_path = tmp_path / "waggle_mcp-0.1.22-py3-none-any.whl"
    write_wheel_fixture(wheel_path, readme)

    assert check_wheel(wheel_path, readme_path) == []


def test_wheel_check_reports_missing_marker_and_readme_drift(tmp_path: Path) -> None:
    readme_path = tmp_path / "README.md"
    readme_path.write_text(f"{MARKER}\n\n# Waggle\n", encoding="utf-8")
    wheel_path = tmp_path / "waggle_mcp-0.1.22-py3-none-any.whl"
    write_wheel_fixture(wheel_path, "# Different description\n")

    assert check_wheel(wheel_path, readme_path) == [
        "wheel METADATA description does not exactly match README.md",
        f"wheel METADATA description is missing {MARKER}",
    ]


@pytest.mark.asyncio
async def test_stdio_smoke_initializes_installed_command_without_protocol_noise(tmp_path: Path) -> None:
    executable = Path(sys.executable).with_name("waggle-mcp")
    assert executable.is_file()

    assert await smoke_stdio(executable, tmp_path) == []
