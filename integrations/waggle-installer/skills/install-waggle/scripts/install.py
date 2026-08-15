#!/usr/bin/env python3
"""Install the official Waggle Codex plugin from its latest stable GitHub release."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPOSITORY = "Abhigyan-Shekhar/Waggle-mcp"
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100"
DOWNLOAD_PREFIX = f"https://github.com/{REPOSITORY}/releases/download/"
ASSET_RE = re.compile(r"^waggle-codex-marketplace-(v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\.zip$")
MAX_DOWNLOAD_BYTES = 1_000_000_000
MAX_ARCHIVE_ENTRIES = 10_000
MAX_EXTRACTED_BYTES = 2_000_000_000
MAX_MARKETPLACE_MANIFEST_BYTES = 1_000_000
USER_AGENT = "waggle-codex-installer/1.0"


class InstallerError(RuntimeError):
    """An expected installation failure with a user-facing message."""


@dataclass(frozen=True)
class ReleaseAsset:
    tag: str
    name: str
    url: str
    digest: str


Runner = Callable[..., subprocess.CompletedProcess[str]]
Opener = Callable[..., object]


def _request_json(url: str, opener: Opener) -> object:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    try:
        with opener(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InstallerError(f"Could not read Waggle releases from GitHub: {exc}") from exc


def discover_latest_stable(opener: Opener = urllib.request.urlopen) -> ReleaseAsset:
    releases = _request_json(RELEASES_API, opener)
    if not isinstance(releases, list):
        raise InstallerError("GitHub returned an unexpected releases response.")

    stable = [
        release
        for release in releases
        if isinstance(release, dict) and not release.get("draft") and not release.get("prerelease")
    ]
    if not stable:
        raise InstallerError("No stable Waggle GitHub Release was found.")

    release = max(stable, key=lambda item: str(item.get("published_at") or item.get("created_at") or ""))
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not re.fullmatch(r"v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", tag):
        raise InstallerError("The latest stable Waggle release has an unsupported tag.")

    expected_name = f"waggle-codex-marketplace-{tag}.zip"
    assets = release.get("assets")
    matches = [asset for asset in assets or [] if isinstance(asset, dict) and asset.get("name") == expected_name]
    if len(matches) != 1:
        raise InstallerError(f"Release {tag} does not contain the expected asset {expected_name}.")

    url = matches[0].get("browser_download_url")
    if (
        not isinstance(url, str)
        or not url.startswith(f"{DOWNLOAD_PREFIX}{tag}/")
        or not url.endswith(f"/{expected_name}")
    ):
        raise InstallerError("GitHub returned an unexpected download URL for the Waggle marketplace bundle.")
    if ASSET_RE.fullmatch(expected_name) is None:
        raise InstallerError("The Waggle marketplace asset name is not recognized.")
    digest = matches[0].get("digest")
    digest_match = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", digest) if isinstance(digest, str) else None
    if digest_match is None:
        raise InstallerError("GitHub did not provide a valid SHA-256 digest for the Waggle marketplace bundle.")
    return ReleaseAsset(tag=tag, name=expected_name, url=url, digest=f"sha256:{digest_match.group(1).lower()}")


def download_asset(asset: ReleaseAsset, destination: Path, opener: Opener = urllib.request.urlopen) -> None:
    request = urllib.request.Request(
        asset.url, headers={"Accept": "application/octet-stream", "User-Agent": USER_AGENT}
    )
    downloaded = 0
    hasher = hashlib.sha256()
    try:
        with opener(request, timeout=60) as response, destination.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                downloaded += len(chunk)
                if downloaded > MAX_DOWNLOAD_BYTES:
                    raise InstallerError("The Waggle release asset exceeds the installer safety limit.")
                hasher.update(chunk)
                output.write(chunk)
        actual_digest = f"sha256:{hasher.hexdigest()}"
        if not hmac.compare_digest(actual_digest, asset.digest):
            raise InstallerError(f"Downloaded {asset.name} does not match GitHub's SHA-256 digest.")
    except InstallerError:
        raise
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise InstallerError(f"Could not download {asset.name}: {exc}") from exc


def _safe_member_path(destination: Path, member_name: str) -> Path:
    if not member_name or "\\" in member_name or "\x00" in member_name:
        raise InstallerError("The Waggle ZIP contains a malformed path.")
    relative = PurePosixPath(member_name)
    if relative.is_absolute() or ".." in relative.parts or (relative.parts and ":" in relative.parts[0]):
        raise InstallerError(f"Unsafe path in Waggle ZIP: {member_name}")
    target = destination.joinpath(*relative.parts)
    try:
        target.resolve().relative_to(destination.resolve())
    except ValueError as exc:
        raise InstallerError(f"Unsafe path in Waggle ZIP: {member_name}") from exc
    return target


def safe_extract(archive: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > MAX_ARCHIVE_ENTRIES:
                raise InstallerError("The Waggle ZIP contains too many entries.")
            if sum(member.file_size for member in members) > MAX_EXTRACTED_BYTES:
                raise InstallerError("The Waggle ZIP is too large when extracted.")

            for member in members:
                target = _safe_member_path(destination, member.filename)
                mode = member.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if file_type == stat.S_IFLNK:
                    raise InstallerError(f"Symbolic links are not allowed in the Waggle ZIP: {member.filename}")
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise InstallerError(f"Unsupported file type in the Waggle ZIP: {member.filename}")

                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                permissions = stat.S_IMODE(mode)
                if permissions:
                    target.chmod(permissions)
    except InstallerError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as exc:
        raise InstallerError(f"The Waggle release asset is not a valid safe ZIP: {exc}") from exc


def find_marketplace_root(extracted: Path) -> Path:
    candidates: list[Path] = []
    for manifest in extracted.rglob("marketplace.json"):
        if manifest.parts[-3:] != (".agents", "plugins", "marketplace.json"):
            continue
        try:
            if manifest.stat().st_size > MAX_MARKETPLACE_MANIFEST_BYTES:
                continue
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        entries = data.get("plugins") if isinstance(data, dict) else None
        waggle = next(
            (entry for entry in entries or [] if isinstance(entry, dict) and entry.get("name") == "waggle"), None
        )
        source = waggle.get("source") if isinstance(waggle, dict) else None
        source_path = source.get("path") if isinstance(source, dict) else None
        if data.get("name") != "waggle" or not isinstance(source_path, str):
            continue

        root = manifest.parents[2]
        plugin_path = (root / source_path).resolve()
        try:
            plugin_path.relative_to(root.resolve())
        except ValueError:
            continue
        plugin_manifest = plugin_path / ".codex-plugin" / "plugin.json"
        if plugin_manifest.is_file():
            candidates.append(root)

    if len(candidates) != 1:
        raise InstallerError("Could not find one valid Waggle marketplace root in the release ZIP.")
    return candidates[0]


def _run_codex(codex: str, arguments: list[str], runner: Runner) -> subprocess.CompletedProcess[str]:
    result = runner([codex, *arguments], capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise InstallerError(f"Command failed: {codex} {' '.join(arguments)}\n{detail}")
    return result


def is_waggle_installed(codex: str, runner: Runner = subprocess.run) -> bool:
    result = _run_codex(codex, ["plugin", "list", "--json"], runner)
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise InstallerError("Codex returned invalid JSON while checking installed plugins.") from exc
    installed = payload.get("installed") if isinstance(payload, dict) else None
    return any(
        isinstance(plugin, dict)
        and plugin.get("installed") is True
        and (
            plugin.get("pluginId") == "waggle@waggle"
            or (plugin.get("name") == "waggle" and plugin.get("marketplaceName") == "waggle")
        )
        for plugin in installed or []
    )


def default_install_base() -> Path:
    codex_home = Path(os.environ["CODEX_HOME"]).expanduser() if os.environ.get("CODEX_HOME") else Path.home() / ".codex"
    return codex_home / "marketplaces" / "waggle"


def install(
    *,
    codex: str,
    install_base: Path,
    opener: Opener = urllib.request.urlopen,
    runner: Runner = subprocess.run,
) -> str:
    if is_waggle_installed(codex, runner):
        return "Waggle is already installed."

    asset = discover_latest_stable(opener)
    print(f"Downloading official Waggle release asset: {asset.name}")
    install_base.mkdir(parents=True, exist_ok=True)
    release_dir = install_base / asset.tag

    if release_dir.exists():
        if release_dir.is_symlink() or not release_dir.is_dir():
            raise InstallerError(f"Existing install path is not a safe directory: {release_dir}")
        marketplace_root = find_marketplace_root(release_dir)
    else:
        staging = Path(tempfile.mkdtemp(prefix=f".{asset.tag}-", dir=install_base))
        try:
            with tempfile.TemporaryDirectory(prefix="waggle-download-") as download_dir:
                archive = Path(download_dir) / asset.name
                download_asset(asset, archive, opener)
                safe_extract(archive, staging)
            relative_root = find_marketplace_root(staging).relative_to(staging)
            staging.rename(release_dir)
            marketplace_root = release_dir / relative_root
        except Exception:
            if staging.exists() and staging.parent == install_base and staging.name.startswith(f".{asset.tag}-"):
                shutil.rmtree(staging, ignore_errors=True)
            raise

    _run_codex(codex, ["plugin", "marketplace", "add", str(marketplace_root)], runner)
    _run_codex(codex, ["plugin", "add", "waggle@waggle"], runner)
    if not is_waggle_installed(codex, runner):
        raise InstallerError("Codex completed the install command, but waggle@waggle is not listed as installed.")
    return f"Waggle {asset.tag} installed successfully. Start a new Codex task to load it."


def main() -> int:
    codex = shutil.which("codex")
    if codex is None:
        print(
            "Waggle installation requires a supported local Codex environment with the codex CLI available.",
            file=sys.stderr,
        )
        return 1
    try:
        print(install(codex=codex, install_base=default_install_base()))
    except InstallerError as exc:
        print(f"Waggle installation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
