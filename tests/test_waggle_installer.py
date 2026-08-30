from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).parents[1]
    / "integrations"
    / "waggle-installer"
    / "skills"
    / "install-waggle"
    / "scripts"
    / "install.py"
)
SPEC = importlib.util.spec_from_file_location("waggle_installer", SCRIPT)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


class Response:
    def __init__(self, data: bytes):
        self.stream = io.BytesIO(data)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)


def release_payload(*, asset: bool = True, digest: str | None = None) -> list[dict]:
    assets = []
    if asset:
        assets.append(
            {
                "name": "waggle-codex-marketplace-v9.8.7.zip",
                "browser_download_url": (
                    "https://github.com/Abhigyan-Shekhar/Waggle-mcp/releases/download/"
                    "v9.8.7/waggle-codex-marketplace-v9.8.7.zip"
                ),
                "digest": digest or f"sha256:{hashlib.sha256(MARKETPLACE_ARCHIVE).hexdigest()}",
            }
        )
    return [
        {"tag_name": "v10.0.0", "draft": True, "prerelease": False, "published_at": "2030-03-01", "assets": []},
        {"tag_name": "v9.9.0", "draft": False, "prerelease": True, "published_at": "2030-02-01", "assets": []},
        {
            "tag_name": "v9.8.7",
            "draft": False,
            "prerelease": False,
            "published_at": "2030-01-01",
            "assets": assets,
        },
    ]


def marketplace_zip() -> bytes:
    output = io.BytesIO()
    marketplace = {
        "name": "waggle",
        "plugins": [
            {
                "name": "waggle",
                "source": {"source": "local", "path": "./plugins/waggle"},
            }
        ],
    }
    with zipfile.ZipFile(output, "w") as bundle:
        root = "waggle-codex-marketplace-v9.8.7"
        bundle.writestr(f"{root}/.agents/plugins/marketplace.json", json.dumps(marketplace))
        bundle.writestr(f"{root}/plugins/waggle/.codex-plugin/plugin.json", '{"name":"waggle"}')
    return output.getvalue()


MARKETPLACE_ARCHIVE = marketplace_zip()


def opener_for(payload: list[dict], archive: bytes = b""):
    def open_url(request, timeout):
        del timeout
        if request.full_url == installer.RELEASES_API:
            return Response(json.dumps(payload).encode())
        return Response(archive)

    return open_url


class CodexRunner:
    def __init__(self, *, initially_installed: bool = False, fail_at: tuple[str, ...] | None = None):
        self.installed = initially_installed
        self.fail_at = fail_at
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command, **_kwargs):
        call = tuple(command[1:])
        self.calls.append(call)
        if call == self.fail_at:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="simulated failure")
        if call == ("plugin", "add", "waggle@waggle"):
            self.installed = True
        if call == ("plugin", "list", "--json"):
            plugins = (
                [{"pluginId": "waggle@waggle", "name": "waggle", "marketplaceName": "waggle", "installed": True}]
                if self.installed
                else []
            )
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"installed": plugins}), stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")


def test_already_installed_stops_before_network(tmp_path):
    runner = CodexRunner(initially_installed=True)

    def no_network(*_args, **_kwargs):
        raise AssertionError("network should not be used")

    result = installer.install(codex="codex", install_base=tmp_path, opener=no_network, runner=runner)

    assert result == "Waggle is already installed."
    assert runner.calls == [("plugin", "list", "--json")]


def test_fresh_install_uses_existing_codex_commands(tmp_path):
    runner = CodexRunner()
    result = installer.install(
        codex="codex",
        install_base=tmp_path,
        opener=opener_for(release_payload(), MARKETPLACE_ARCHIVE),
        runner=runner,
    )

    marketplace_root = tmp_path / "v9.8.7" / "waggle-codex-marketplace-v9.8.7"
    assert result.startswith("Waggle v9.8.7 installed successfully.")
    assert (marketplace_root / ".agents/plugins/marketplace.json").is_file()
    assert ("plugin", "marketplace", "add", str(marketplace_root)) in runner.calls
    assert ("plugin", "add", "waggle@waggle") in runner.calls


def test_latest_stable_release_ignores_drafts_and_prereleases():
    asset = installer.discover_latest_stable(opener_for(release_payload()))

    assert asset.tag == "v9.8.7"
    assert asset.name == "waggle-codex-marketplace-v9.8.7.zip"
    assert asset.digest == f"sha256:{hashlib.sha256(MARKETPLACE_ARCHIVE).hexdigest()}"


def test_missing_release_asset_fails():
    with pytest.raises(installer.InstallerError, match="does not contain the expected asset"):
        installer.discover_latest_stable(opener_for(release_payload(asset=False)))


def test_download_digest_mismatch_fails(tmp_path):
    asset = installer.discover_latest_stable(opener_for(release_payload(digest=f"sha256:{'0' * 64}")))

    with pytest.raises(installer.InstallerError, match="does not match GitHub's SHA-256 digest"):
        installer.download_asset(asset, tmp_path / "bundle.zip", opener_for([], MARKETPLACE_ARCHIVE))


def test_malformed_zip_fails(tmp_path):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(b"not a zip")

    with pytest.raises(installer.InstallerError, match="not a valid safe ZIP"):
        installer.safe_extract(archive, tmp_path / "out")


def test_path_traversal_zip_fails(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape", "no")

    with pytest.raises(installer.InstallerError, match="Unsafe path"):
        installer.safe_extract(archive, tmp_path / "out")


def test_oversized_marketplace_manifest_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / "waggle-marketplace"
    manifest = root / ".agents" / "plugins" / "marketplace.json"
    plugin_manifest = root / "plugins" / "waggle" / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    plugin_manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "name": "waggle",
                "plugins": [{"name": "waggle", "source": {"source": "local", "path": "./plugins/waggle"}}],
            }
        )
    )
    plugin_manifest.write_text('{"name":"waggle"}')
    monkeypatch.setattr(installer, "MAX_MARKETPLACE_MANIFEST_BYTES", manifest.stat().st_size - 1)

    with pytest.raises(installer.InstallerError, match="Could not find one valid Waggle marketplace root"):
        installer.find_marketplace_root(tmp_path)


def test_marketplace_command_failure_is_reported(tmp_path):
    runner = CodexRunner(
        fail_at=("plugin", "marketplace", "add", str(tmp_path / "v9.8.7" / "waggle-codex-marketplace-v9.8.7"))
    )

    with pytest.raises(installer.InstallerError, match="plugin marketplace add"):
        installer.install(
            codex="codex",
            install_base=tmp_path,
            opener=opener_for(release_payload(), MARKETPLACE_ARCHIVE),
            runner=runner,
        )


def test_plugin_install_failure_is_reported(tmp_path):
    runner = CodexRunner(fail_at=("plugin", "add", "waggle@waggle"))

    with pytest.raises(installer.InstallerError, match="plugin add waggle@waggle"):
        installer.install(
            codex="codex",
            install_base=tmp_path,
            opener=opener_for(release_payload(), MARKETPLACE_ARCHIVE),
            runner=runner,
        )
