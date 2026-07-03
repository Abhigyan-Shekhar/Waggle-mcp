from __future__ import annotations

import json
from pathlib import Path

import pytest

from waggle.server import (
    _build_parser,
    _write_antigravity,
    _write_claude_desktop,
    _write_codex,
    _write_cursor,
    _write_other,
)


def test_parser_accepts_explicit_serve_transport_override() -> None:
    parser = _build_parser()
    args = parser.parse_args(["serve", "--transport", "stdio"])

    assert args.command == "serve"
    assert args.transport == "stdio"


@pytest.mark.parametrize("command_name", ["graph-studio", "open-studio"])
def test_parser_exposes_graph_studio_aliases(command_name: str) -> None:
    parser = _build_parser()
    args = parser.parse_args([command_name, "--port", "8787", "--no-open"])

    assert args.command == command_name
    assert args.port == 8787
    assert args.open is False


def test_distribution_commands_present_in_help(capsys):
    parser = _build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--help"])

    output = capsys.readouterr().out

    for command in ("export", "push", "pull", "share"):
        assert command in output


@pytest.mark.parametrize(
    ("command", "expected_flags"),
    [
        (
            "export",
            [
                "--output",
                "--scope",
                "--project",
                "--session-id",
            ],
        ),
        (
            "push",
            [
                "--drive",
                "--folder-id",
                "--client-secret-path",
                "--token-path",
            ],
        ),
        (
            "pull",
            [
                "--folder-id",
                "--client-secret-path",
                "--token-path",
                "--merge-strategy",
            ],
        ),
        (
            "share",
            [
                "--folder-id",
                "--client-secret-path",
                "--token-path",
            ],
        ),
    ],
)
def test_distribution_command_help_contains_expected_flags(
    command: str,
    expected_flags: list[str],
    capsys,
):
    parser = _build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([command, "--help"])

    output = capsys.readouterr().out

    for flag in expected_flags:
        assert flag in output, f"Expected flag {flag!r} missing from `{command} --help`"


def test_write_codex_config_uses_packaged_stdio_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    config_path = _write_codex(str(tmp_path / "memory.db"), "/tmp/fake-python")
    contents = config_path.read_text()

    assert 'command = "waggle-mcp"' in contents
    assert 'args = ["serve", "--transport", "stdio"]' in contents
    assert "WAGGLE_TRANSPORT" not in contents


@pytest.mark.parametrize(
    ("writer", "relative_path"),
    [
        (_write_claude_desktop, ".config/claude/claude_desktop_config.json"),
        (_write_cursor, ".cursor/mcp.json"),
        (_write_antigravity, ".gemini/antigravity/mcp_config.json"),
        (_write_other, "waggle-mcp-config.json"),
    ],
)
def test_json_client_writers_use_packaged_stdio_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    writer,
    relative_path: str,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("waggle.server.sys.platform", "linux")

    config_path = writer(str(tmp_path / "memory.db"), "/tmp/fake-python")
    payload = json.loads(config_path.read_text())

    assert config_path == tmp_path / relative_path
    if relative_path == "waggle-mcp-config.json":
        assert payload["command"] == "waggle-mcp"
        assert payload["args"] == ["serve", "--transport", "stdio"]
        assert "WAGGLE_TRANSPORT" not in payload["env"]
    else:
        assert payload["mcpServers"]["waggle"]["command"] == "waggle-mcp"
        assert payload["mcpServers"]["waggle"]["args"] == ["serve", "--transport", "stdio"]
        assert "WAGGLE_TRANSPORT" not in payload["mcpServers"]["waggle"]["env"]
