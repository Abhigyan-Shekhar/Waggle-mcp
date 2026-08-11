from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from scripts.check_registry_readiness import (
    check_artifacts,
    check_manifest,
    check_project_metadata,
    check_schema,
    check_wheel,
    main,
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


VALID_WHEEL_MEMBERS = {
    "waggle/__init__.py": b"",
    "rlm/__init__.py": b"",
    "waggle_mcp-0.1.22.dist-info/METADATA": b"Metadata-Version: 2.4\nName: waggle-mcp\nVersion: 0.1.22\n",
    "waggle_mcp-0.1.22.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
    "waggle_mcp-0.1.22.dist-info/RECORD": b"",
}
VALID_SDIST_MEMBERS = {
    "waggle_mcp-0.1.22/README.md": b"# Waggle\n",
    "waggle_mcp-0.1.22/pyproject.toml": b"[project]\nname = 'waggle-mcp'\nversion = '0.1.22'\n",
    "waggle_mcp-0.1.22/PKG-INFO": b"Metadata-Version: 2.4\nName: waggle-mcp\nVersion: 0.1.22\n",
    "waggle_mcp-0.1.22/src/waggle/__init__.py": b"",
    "waggle_mcp-0.1.22/src/rlm/__init__.py": b"",
}


def write_artifact_pair(
    dist_dir: Path,
    *,
    wheel_members: dict[str, bytes] | None = None,
    sdist_members: dict[str, bytes] | None = None,
) -> tuple[Path, Path]:
    dist_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = dist_dir / "waggle_mcp-0.1.22-py3-none-any.whl"
    with ZipFile(wheel_path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in (wheel_members or VALID_WHEEL_MEMBERS).items():
            archive.writestr(name, content)

    sdist_path = dist_dir / "waggle_mcp-0.1.22.tar.gz"
    with tarfile.open(sdist_path, "w:gz") as archive:
        for name, content in (sdist_members or VALID_SDIST_MEMBERS).items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return wheel_path, sdist_path


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


def test_manifest_check_reports_schema_and_project_metadata_issues(tmp_path: Path) -> None:
    schema_path = write_repository_fixture(tmp_path)
    manifest_path = tmp_path / "server.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["title"] = "Not Waggle"
    manifest["packages"][0]["identifier"] = "different-package"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert check_manifest(tmp_path, schema_path) == [
        "server.json title: 'Waggle' was expected",
        "server.json PyPI package 'different-package' does not match pyproject.toml [project].name 'waggle-mcp'",
    ]


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


def test_artifacts_check_accepts_one_safe_wheel_and_sdist(tmp_path: Path) -> None:
    write_artifact_pair(tmp_path)

    assert check_artifacts(tmp_path) == []


def test_artifacts_check_requires_exactly_one_wheel_and_sdist(tmp_path: Path) -> None:
    assert check_artifacts(tmp_path) == [
        "distribution directory must contain exactly one .tar.gz source distribution; found 0",
        "distribution directory must contain exactly one wheel; found 0",
    ]

    wheel, sdist = write_artifact_pair(tmp_path)
    shutil.copyfile(wheel, tmp_path / "second.whl")
    shutil.copyfile(sdist, tmp_path / "second.tar.gz")

    assert check_artifacts(tmp_path) == [
        "distribution directory must contain exactly one .tar.gz source distribution; found 2",
        "distribution directory must contain exactly one wheel; found 2",
    ]


@pytest.mark.parametrize(
    "member",
    [
        "/absolute.txt",
        "C:/outside.txt",
        "../outside.txt",
        "waggle/../../outside.txt",
        "waggle/./module.py",
        "waggle//module.py",
    ],
)
def test_artifacts_check_rejects_unsafe_wheel_paths(tmp_path: Path, member: str) -> None:
    members = {**VALID_WHEEL_MEMBERS, member: b"unsafe"}
    write_artifact_pair(tmp_path, wheel_members=members)

    issues = check_artifacts(tmp_path)

    assert any("wheel contains unsafe path" in issue and member in issue for issue in issues)


@pytest.mark.parametrize(
    "member",
    [
        "/absolute.txt",
        "../outside.txt",
        "waggle_mcp-0.1.22/src/waggle/../../../../outside.txt",
    ],
)
def test_artifacts_check_rejects_unsafe_sdist_paths(tmp_path: Path, member: str) -> None:
    members = {**VALID_SDIST_MEMBERS, member: b"unsafe"}
    write_artifact_pair(tmp_path, sdist_members=members)

    issues = check_artifacts(tmp_path)

    assert any("sdist contains unsafe path" in issue and member in issue for issue in issues)


@pytest.mark.parametrize(
    "member",
    [
        "waggle/__pycache__/module.pyc",
        "waggle/module.pyc",
        "waggle/.pytest_cache/state",
        ".DS_Store",
        ".coverage",
        "htmlcov/index.html",
        ".env",
        ".venv/bin/python",
        "data/local.db",
        "data/local.sqlite",
        "data/local.sqlite3",
        "config/credentials.json",
        "config/credentials/token",
        "config/.env.production/token",
        "config/private.pem",
        "config/private.key",
        ".ssh/id_rsa",
        ".git/config",
        "benchmark_results/report.json",
        "build/output.txt",
        "dist/package.whl",
        "dist-release/package.whl",
        "graph-ui/package.json",
        "node_modules/package/index.js",
        "tests/test_leak.py",
    ],
)
def test_artifacts_check_rejects_forbidden_wheel_members(tmp_path: Path, member: str) -> None:
    members = {**VALID_WHEEL_MEMBERS, member: b"forbidden"}
    write_artifact_pair(tmp_path, wheel_members=members)

    issues = check_artifacts(tmp_path)

    assert any("wheel contains forbidden member" in issue and member in issue for issue in issues)


@pytest.mark.parametrize(
    "member",
    [
        "waggle_mcp-0.1.22/src/waggle/__pycache__/module.pyc",
        "waggle_mcp-0.1.22/.env",
        "waggle_mcp-0.1.22/data/local.db",
        "waggle_mcp-0.1.22/config/credentials.json",
        "waggle_mcp-0.1.22/config/credentials/token",
        "waggle_mcp-0.1.22/config/.env.production/token",
        "waggle_mcp-0.1.22/config/private.key",
        "waggle_mcp-0.1.22/tests/test_leak.py",
    ],
)
def test_artifacts_check_rejects_forbidden_sdist_members(tmp_path: Path, member: str) -> None:
    members = {**VALID_SDIST_MEMBERS, member: b"forbidden"}
    write_artifact_pair(tmp_path, sdist_members=members)

    issues = check_artifacts(tmp_path)

    assert any("sdist contains forbidden member" in issue and member in issue for issue in issues)


def test_artifacts_check_rejects_backslash_paths_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    member = "waggle\\..\\outside.txt"
    members = {**VALID_WHEEL_MEMBERS, member: b"unsafe"}
    write_artifact_pair(tmp_path, wheel_members=members)

    monkeypatch.setattr("zipfile.os.sep", "\\")

    assert any("wheel contains unsafe path" in issue and member in issue for issue in check_artifacts(tmp_path))


def test_artifacts_check_requires_wheel_metadata_and_package_roots(tmp_path: Path) -> None:
    members = {
        name: content
        for name, content in VALID_WHEEL_MEMBERS.items()
        if not name.endswith("/METADATA") and name != "waggle/__init__.py"
    }
    write_artifact_pair(tmp_path, wheel_members=members)

    issues = check_artifacts(tmp_path)

    assert "wheel must contain exactly one .dist-info/METADATA file; found 0" in issues
    assert "wheel must contain the waggle package root" in issues


@pytest.mark.parametrize(
    ("removed_member", "expected_issue"),
    [
        (
            "waggle_mcp-0.1.22/pyproject.toml",
            "sdist must contain pyproject.toml at its package root",
        ),
        (
            "waggle_mcp-0.1.22/src/waggle/__init__.py",
            "sdist must contain the src/waggle package root",
        ),
    ],
)
def test_artifacts_check_requires_sdist_metadata_and_package_roots(
    tmp_path: Path,
    removed_member: str,
    expected_issue: str,
) -> None:
    members = {name: content for name, content in VALID_SDIST_MEMBERS.items() if name != removed_member}
    write_artifact_pair(tmp_path, sdist_members=members)

    assert expected_issue in check_artifacts(tmp_path)


def test_artifacts_check_rejects_multiple_sdist_roots(tmp_path: Path) -> None:
    members = {**VALID_SDIST_MEMBERS, "other-project/README.md": b"# Other\n"}
    write_artifact_pair(tmp_path, sdist_members=members)

    assert "sdist must contain exactly one top-level directory; found 2" in check_artifacts(tmp_path)


def test_artifacts_cli_reports_validation_result(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_artifact_pair(tmp_path)

    assert main(["artifacts", "--dist-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "Registry readiness validation passed.\n"


@pytest.mark.asyncio
async def test_stdio_smoke_initializes_installed_command_without_protocol_noise(tmp_path: Path) -> None:
    executable = shutil.which("waggle-mcp")
    assert executable is not None

    assert await smoke_stdio(Path(executable), tmp_path) == []
