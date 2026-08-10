from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest
from pytest import CaptureFixture, MonkeyPatch

NAME = "io.github.Abhigyan-Shekhar/Waggle-mcp"


def sync_module() -> ModuleType:
    return importlib.import_module("scripts.sync_release_metadata")


def write_repo(
    root: Path,
    *,
    project_version: str = "0.1.22",
    server_version: str = "0.1.22",
    package_versions: tuple[str, ...] = ("0.1.22",),
    marker: str | None = NAME,
) -> None:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "waggle-mcp"\nversion = "{project_version}"\n',
        encoding="utf-8",
    )
    server = {
        "$schema": "https://example.invalid/server.schema.json",
        "name": NAME,
        "description": "Keep café metadata unchanged.",
        "version": server_version,
        "packages": [
            {
                "registryType": "pypi",
                "identifier": f"waggle-mcp-{index}",
                "version": version,
                "transport": {"type": "stdio"},
            }
            for index, version in enumerate(package_versions)
        ],
    }
    (root / "server.json").write_text(
        json.dumps(server, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    prefix = f"<!-- mcp-name: {marker} -->\n\n" if marker is not None else ""
    (root / "README.md").write_text(prefix + "# Waggle\n", encoding="utf-8")


def test_check_passes_for_consistent_metadata(tmp_path: Path) -> None:
    write_repo(tmp_path)
    module = sync_module()

    assert module.check_metadata(tmp_path) == []
    assert module.main(["--check"], root=tmp_path) == 0


def test_check_reports_every_version_mismatch(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    write_repo(
        tmp_path,
        project_version="0.1.22",
        server_version="0.1.8",
        package_versions=("0.1.8", "0.1.7"),
    )
    module = sync_module()

    assert module.main(["--check"], root=tmp_path) == 1
    output = capsys.readouterr().out
    assert "server.json .version: expected 0.1.22, found 0.1.8" in output
    assert "server.json .packages[0].version: expected 0.1.22, found 0.1.8" in output
    assert "server.json .packages[1].version: expected 0.1.22, found 0.1.7" in output


def test_check_reports_missing_marker(tmp_path: Path) -> None:
    write_repo(tmp_path, marker=None)

    assert sync_module().check_metadata(tmp_path) == [f"README.md: missing <!-- mcp-name: {NAME} -->"]


def test_check_reports_mismatched_marker(tmp_path: Path) -> None:
    write_repo(tmp_path, marker="io.github.someone-else/waggle-mcp")

    issues = sync_module().check_metadata(tmp_path)

    assert len(issues) == 1
    assert "does not match server.json .name" in issues[0]


def test_check_reports_duplicate_markers(tmp_path: Path) -> None:
    write_repo(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + f"<!-- mcp-name: {NAME} -->\n",
        encoding="utf-8",
    )

    assert sync_module().check_metadata(tmp_path) == ["README.md: found 2 mcp-name markers; expected exactly one"]


def test_check_rejects_empty_packages_array(tmp_path: Path) -> None:
    write_repo(tmp_path, package_versions=())

    assert sync_module().check_metadata(tmp_path) == ["server.json .packages must declare at least one package"]


def test_check_reports_malformed_metadata(tmp_path: Path) -> None:
    write_repo(tmp_path)
    (tmp_path / "server.json").write_text("not json\n", encoding="utf-8")

    issues = sync_module().check_metadata(tmp_path)

    assert len(issues) == 1
    assert issues[0].startswith("metadata validation failed:")


def test_default_root_is_resolved_inside_each_call(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    write_repo(first, project_version="0.1.22", server_version="0.1.22")
    write_repo(second, project_version="0.1.23", server_version="0.1.22")
    module = sync_module()

    monkeypatch.setattr(
        module,
        "__file__",
        str(first / "scripts" / "sync_release_metadata.py"),
    )
    assert module.check_metadata() == []

    monkeypatch.setattr(
        module,
        "__file__",
        str(second / "scripts" / "sync_release_metadata.py"),
    )
    assert module.check_metadata() == [
        "server.json .version: expected 0.1.23, found 0.1.22",
        "server.json .packages[0].version: expected 0.1.23, found 0.1.22",
    ]


def test_write_changes_only_generated_version_lines(tmp_path: Path) -> None:
    write_repo(
        tmp_path,
        project_version="0.1.22",
        server_version="0.1.8",
        package_versions=("0.1.8", "0.1.7"),
    )
    server_path = tmp_path / "server.json"
    before = server_path.read_bytes()
    expected = before.replace(b'"version": "0.1.8"', b'"version": "0.1.22"')
    expected = expected.replace(b'"version": "0.1.7"', b'"version": "0.1.22"')

    assert sync_module().write_metadata(tmp_path) == []
    assert server_path.read_bytes() == expected
    assert b"Keep caf\xc3\xa9 metadata unchanged." in server_path.read_bytes()


def test_write_inserts_missing_marker_from_server_name(tmp_path: Path) -> None:
    write_repo(tmp_path, marker=None)
    readme_path = tmp_path / "README.md"

    assert sync_module().write_metadata(tmp_path) == []
    assert readme_path.read_bytes() == f"<!-- mcp-name: {NAME} -->\n\n# Waggle\n".encode()


def test_write_leaves_matching_marker_byte_identical(tmp_path: Path) -> None:
    write_repo(tmp_path)
    readme_path = tmp_path / "README.md"
    before = readme_path.read_bytes()

    assert sync_module().write_metadata(tmp_path) == []
    assert readme_path.read_bytes() == before


def test_write_never_overwrites_mismatched_marker(tmp_path: Path) -> None:
    wrong_name = "io.github.someone-else/waggle-mcp"
    write_repo(
        tmp_path,
        project_version="0.1.22",
        server_version="0.1.8",
        marker=wrong_name,
    )
    readme_path = tmp_path / "README.md"
    readme_before = readme_path.read_bytes()

    issues = sync_module().write_metadata(tmp_path)

    assert readme_path.read_bytes() == readme_before
    assert json.loads((tmp_path / "server.json").read_text())["version"] == "0.1.22"
    assert len(issues) == 1
    assert "does not match server.json .name" in issues[0]


def test_write_never_overwrites_duplicate_markers(tmp_path: Path) -> None:
    write_repo(tmp_path, server_version="0.1.8")
    readme_path = tmp_path / "README.md"
    readme_path.write_text(
        readme_path.read_text(encoding="utf-8") + f"<!-- mcp-name: {NAME} -->\n",
        encoding="utf-8",
    )
    readme_before = readme_path.read_bytes()

    issues = sync_module().write_metadata(tmp_path)

    assert readme_path.read_bytes() == readme_before
    assert json.loads((tmp_path / "server.json").read_text())["version"] == "0.1.22"
    assert issues == ["README.md: found 2 mcp-name markers; expected exactly one"]


def test_write_refuses_noncanonical_json_without_touching_files(tmp_path: Path) -> None:
    write_repo(tmp_path, project_version="0.1.22", server_version="0.1.8", marker=None)
    server_path = tmp_path / "server.json"
    readme_path = tmp_path / "README.md"
    server_path.write_bytes(server_path.read_bytes().replace(b"\n", b"\r\n"))
    before = {server_path: server_path.read_bytes(), readme_path: readme_path.read_bytes()}

    issues = sync_module().write_metadata(tmp_path)

    assert issues == ["server.json is not in canonical two-space JSON format; refusing to rewrite unrelated bytes"]
    assert {path: path.read_bytes() for path in before} == before


def test_write_cli_returns_one_when_marker_remains_unresolved(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    write_repo(tmp_path, server_version="0.1.8", marker="wrong/name")

    assert sync_module().main(["--write"], root=tmp_path) == 1
    assert "does not match server.json .name" in capsys.readouterr().out


def test_write_cli_returns_zero_after_synchronizing(tmp_path: Path) -> None:
    write_repo(tmp_path, server_version="0.1.8", marker=None)

    assert sync_module().main(["--write"], root=tmp_path) == 0
    assert sync_module().main(["--check"], root=tmp_path) == 0


@pytest.mark.parametrize("argv", [[], ["--check", "--write"]])
def test_cli_requires_exactly_one_mode(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        sync_module().main(argv)

    assert exc_info.value.code == 2
