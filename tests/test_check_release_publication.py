from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_release_publication import (
    ReleaseCheckError,
    fetch_json,
    main,
    poll_pypi,
    poll_registry,
    validate_tag,
)

NAME = "io.github.Abhigyan-Shekhar/Waggle-mcp"
VERSION = "0.1.22"


def write_pyproject(root: Path, version: str = VERSION) -> None:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "waggle-mcp"\nversion = "{version}"\n',
        encoding="utf-8",
    )


def registry_payload(
    *,
    name: str = NAME,
    version: str = VERSION,
    identifier: str = "waggle-mcp",
    package_version: str = VERSION,
) -> dict[str, object]:
    return {
        "server": {
            "name": name,
            "version": version,
            "packages": [
                {
                    "registryType": "pypi",
                    "identifier": identifier,
                    "version": package_version,
                }
            ],
        }
    }


def test_validate_tag_returns_project_version_for_exact_tag(tmp_path: Path) -> None:
    write_pyproject(tmp_path)

    assert validate_tag(tmp_path, "v0.1.22") == VERSION


@pytest.mark.parametrize("tag", ["0.1.22", "v0.1.21", "release-0.1.22"])
def test_validate_tag_rejects_mismatched_and_unprefixed_tags(tmp_path: Path, tag: str) -> None:
    write_pyproject(tmp_path)

    with pytest.raises(
        ReleaseCheckError,
        match=r"^tag .* does not match project version 0\.1\.22; expected v0\.1\.22$",
    ):
        validate_tag(tmp_path, tag)


def test_pypi_retries_404_then_accepts_exact_project_and_version() -> None:
    responses = iter(
        [
            (404, {}),
            (404, {}),
            (200, {"info": {"name": "waggle-mcp", "version": VERSION}}),
        ]
    )
    calls: list[tuple[str, float]] = []
    sleeps: list[float] = []

    def fake_fetch(url: str, timeout: float) -> tuple[int, object]:
        calls.append((url, timeout))
        return next(responses)

    poll_pypi("waggle-mcp", VERSION, fetch_json=fake_fetch, sleep=sleeps.append)

    assert calls == [
        ("https://pypi.org/pypi/waggle-mcp/0.1.22/json", 10.0),
        ("https://pypi.org/pypi/waggle-mcp/0.1.22/json", 10.0),
        ("https://pypi.org/pypi/waggle-mcp/0.1.22/json", 10.0),
    ]
    assert sleeps == [15.0, 15.0]


def test_pypi_stops_after_twenty_attempts() -> None:
    calls = 0
    sleeps: list[float] = []

    def not_found(_url: str, _timeout: float) -> tuple[int, object]:
        nonlocal calls
        calls += 1
        return 404, {}

    with pytest.raises(
        ReleaseCheckError,
        match=r"^PyPI version waggle-mcp 0\.1\.22 was not visible after 20 attempts$",
    ):
        poll_pypi("waggle-mcp", VERSION, fetch_json=not_found, sleep=sleeps.append)

    assert calls == 20
    assert sleeps == [15.0] * 19


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"info": {"name": "different", "version": VERSION}}, "PyPI project name"),
        ({"info": {"name": "waggle-mcp", "version": "9.9.9"}}, "PyPI project version"),
        ({"not-info": {}}, "PyPI response must contain an info object"),
    ],
)
def test_pypi_rejects_wrong_name_wrong_version_and_malformed_payload(
    payload: object,
    message: str,
) -> None:
    def fake_fetch(_url: str, _timeout: float) -> tuple[int, object]:
        return 200, payload

    with pytest.raises(ReleaseCheckError, match=message):
        poll_pypi("waggle-mcp", VERSION, fetch_json=fake_fetch, sleep=lambda _seconds: None)


def test_registry_url_encodes_namespace_and_accepts_exact_package() -> None:
    calls: list[tuple[str, float]] = []

    def fake_fetch(url: str, timeout: float) -> tuple[int, object]:
        calls.append((url, timeout))
        return 200, registry_payload()

    poll_registry(NAME, VERSION, "waggle-mcp", fetch_json=fake_fetch, sleep=lambda _seconds: None)

    assert calls == [
        (
            "https://registry.modelcontextprotocol.io/v0.1/servers/"
            "io.github.Abhigyan-Shekhar%2FWaggle-mcp/versions/0.1.22",
            10.0,
        )
    ]


def test_registry_retries_404_then_stops_after_twelve_attempts() -> None:
    calls = 0
    sleeps: list[float] = []

    def not_found(_url: str, _timeout: float) -> tuple[int, object]:
        nonlocal calls
        calls += 1
        return 404, {}

    with pytest.raises(
        ReleaseCheckError,
        match=r"^MCP Registry version .* 0\.1\.22 was not visible after 12 attempts$",
    ):
        poll_registry(NAME, VERSION, "waggle-mcp", fetch_json=not_found, sleep=sleeps.append)

    assert calls == 12
    assert sleeps == [10.0] * 11


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (registry_payload(name="io.github.example/other"), "Registry server name"),
        (registry_payload(version="9.9.9"), "Registry server version"),
        (registry_payload(identifier="different-package"), "Registry PyPI package identifier"),
        (registry_payload(package_version="9.9.9"), "Registry PyPI package version"),
        ({"not-server": {}}, "Registry response must contain a server object"),
    ],
)
def test_registry_rejects_wrong_name_version_identifier_and_package_version(
    payload: object,
    message: str,
) -> None:
    def fake_fetch(_url: str, _timeout: float) -> tuple[int, object]:
        return 200, payload

    with pytest.raises(ReleaseCheckError, match=message):
        poll_registry(NAME, VERSION, "waggle-mcp", fetch_json=fake_fetch, sleep=lambda _seconds: None)


def test_cli_tag_prints_only_version_on_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_pyproject(tmp_path)

    assert main(["tag", "--tag", "v0.1.22"], root=tmp_path) == 0
    captured = capsys.readouterr()

    assert captured.out == "0.1.22\n"
    assert captured.err == ""


def test_cli_failure_is_nonzero_and_deterministic(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_pyproject(tmp_path)

    assert main(["tag", "--tag", "v0.1.21"], root=tmp_path) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err == (
        "Release publication check failed: tag v0.1.21 does not match project version 0.1.22; expected v0.1.22\n"
    )


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_fetch_json_rejects_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def opener(*_args: object, **_kwargs: object) -> FakeResponse:
        return FakeResponse(b"not-json")

    monkeypatch.setattr("scripts.check_release_publication.urlopen", opener)

    with pytest.raises(ReleaseCheckError, match="response was not valid JSON"):
        fetch_json("https://example.invalid/data", 10.0)
