from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins" / "waggle"
RUNTIME_ROOT = PLUGIN_ROOT / "runtime"
LAUNCHER_PATH = PLUGIN_ROOT / "bin" / "waggle-server-launcher.js"
ENTRYPOINT = ROOT / "src" / "waggle" / "entrypoints" / "server_only.py"
MAX_BINARY_BYTES = 80 * 1024 * 1024
STARTUP_TIMEOUT_SECONDS = 3.0
BUILD_TIMEOUT_SECONDS = 600.0
BUNDLE_MODE = "onedir"

HEAVY_EXCLUDES = [
    # The bundled Codex runtime must stay small and fast to launch. These
    # libraries are optional at runtime because EmbeddingModel falls back to
    # deterministic embeddings when sentence-transformers cannot be imported.
    "sentence_transformers",
    "torch",
    "transformers",
    "sklearn",
    "scipy",
    "huggingface_hub",
    "tokenizers",
    "safetensors",
    "numpy.random",
    "numpy.fft",
    "numpy.testing",
    "numpy.f2py",
    # Optional integrations and developer/visualization paths are not required
    # for the bundled stdio MCP runtime. Their runtime tools degrade with clear
    # errors when the optional packages are unavailable.
    "googleapiclient",
    "google_auth_httplib2",
    "google.genai",
    "pyvis",
    "IPython",
    "jedi",
    "openai",
    "anthropic",
    "portkey_ai",
]

TARGETS: dict[str, str] = {
    "darwin-arm64": "waggle-server",
    "darwin-x86_64": "waggle-server",
    "linux-x86_64": "waggle-server",
    "linux-aarch64": "waggle-server",
    "win32-x86_64": "waggle-server.exe",
}


def _current_target() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        arch = "arm64" if machine == "arm64" else "x86_64"
        return f"darwin-{arch}"
    if system == "linux":
        arch = "aarch64" if machine in {"aarch64", "arm64"} else "x86_64"
        return f"linux-{arch}"
    if system == "windows":
        return "win32-x86_64"
    raise SystemExit(f"Unsupported build platform: {platform.system()} {platform.machine()}")


def _binary_path(target: str) -> Path:
    return RUNTIME_ROOT / target / TARGETS[target]


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())


def _run(command: list[str], *, cwd: Path = ROOT, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False, timeout=timeout)


def build_current() -> Path:
    target = _current_target()
    output_dir = _binary_path(target).parent
    output_dir.mkdir(parents=True, exist_ok=True)
    name = TARGETS[target].removesuffix(".exe")

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        name,
        *[flag for module in HEAVY_EXCLUDES for flag in ("--exclude-module", module)],
        str(ENTRYPOINT),
    ]
    try:
        result = _run(command, timeout=BUILD_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"PyInstaller build exceeded {BUILD_TIMEOUT_SECONDS:.0f}s: {' '.join(exc.cmd)}") from exc
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)

    output_dir = _binary_path(target).parent
    if BUNDLE_MODE == "onedir":
        built_dir = ROOT / "dist" / name
        if not built_dir.exists():
            raise SystemExit(f"PyInstaller output directory missing: {built_dir}")
        if output_dir.exists():
            for path in output_dir.iterdir():
                if path.name == ".gitkeep":
                    continue
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        shutil.copytree(built_dir, output_dir, dirs_exist_ok=True)
    else:
        built = ROOT / "dist" / TARGETS[target]
        if not built.exists() and sys.platform == "win32":
            built = ROOT / "dist" / f"{name}.exe"
        destination = _binary_path(target)
        shutil.copy2(built, destination)
    destination = _binary_path(target)
    if destination.suffix != ".exe":
        destination.chmod(destination.stat().st_mode | 0o111)
    if sys.platform == "darwin":
        _run(["xattr", "-cr", str(output_dir)])
        # Desktop/iCloud/FileProvider-backed workspaces can attach provenance
        # metadata that makes the very first execution pay filesystem hydration
        # cost. Touch extended-attribute metadata before the startup probe so
        # CI/local validation measures the runtime, not provider bookkeeping.
        _run(["xattr", "-lr", str(output_dir)])
    return destination


def validate_layout(require_artifacts: bool, probe: bool, verify_signatures: bool) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    current_target = _current_target()

    if not LAUNCHER_PATH.exists():
        failures.append(f"Missing launcher: {LAUNCHER_PATH.relative_to(ROOT)}")

    for target, executable in TARGETS.items():
        target_dir = RUNTIME_ROOT / target
        if not target_dir.exists():
            failures.append(f"Missing runtime target directory: {target_dir.relative_to(ROOT)}")
            continue

        binary = target_dir / executable
        if not binary.exists():
            if require_artifacts:
                failures.append(f"Missing runtime artifact: {binary.relative_to(ROOT)}")
            continue

        size = binary.stat().st_size
        if size > MAX_BINARY_BYTES:
            failures.append(f"{binary.relative_to(ROOT)} is {size} bytes; limit is {MAX_BINARY_BYTES}")
        target_size = _path_size(target_dir)
        if target_size > MAX_BINARY_BYTES:
            failures.append(f"{target_dir.relative_to(ROOT)} is {target_size} bytes; limit is {MAX_BINARY_BYTES}")

        if probe and target == current_target:
            started_at = time.monotonic()
            try:
                result = _run([str(binary), "--server-info"], timeout=STARTUP_TIMEOUT_SECONDS)
                elapsed = time.monotonic() - started_at
            except subprocess.TimeoutExpired:
                if target.startswith("darwin-"):
                    result = None
                    elapsed = STARTUP_TIMEOUT_SECONDS
                    for attempt in range(2, 9):
                        time.sleep(0.25)
                        retry_started_at = time.monotonic()
                        try:
                            retry = _run([str(binary), "--server-info"], timeout=STARTUP_TIMEOUT_SECONDS)
                        except subprocess.TimeoutExpired:
                            continue
                        retry_elapsed = time.monotonic() - retry_started_at
                        if retry.returncode != 0:
                            failures.append(
                                f"{binary.relative_to(ROOT)} --server-info retry failed: {retry.stderr.strip()}"
                            )
                            break
                        result = retry
                        elapsed = retry_elapsed
                        warnings.append(
                            f"{binary.relative_to(ROOT)} first macOS launch exceeded "
                            f"{STARTUP_TIMEOUT_SECONDS:.1f}s; attempt {attempt} took {retry_elapsed:.2f}s"
                        )
                        break
                    if result is None:
                        failures.append(
                            f"{binary.relative_to(ROOT)} --server-info exceeded "
                            f"{STARTUP_TIMEOUT_SECONDS:.1f}s startup budget after macOS first-run retries"
                        )
                        continue
                else:
                    failures.append(
                        f"{binary.relative_to(ROOT)} --server-info exceeded {STARTUP_TIMEOUT_SECONDS:.1f}s startup budget"
                    )
                    continue
            if result.returncode != 0:
                failures.append(f"{binary.relative_to(ROOT)} --server-info failed: {result.stderr.strip()}")
            elif elapsed > STARTUP_TIMEOUT_SECONDS:
                failures.append(f"{binary.relative_to(ROOT)} startup probe took {elapsed:.2f}s")
            else:
                try:
                    payload = json.loads(result.stdout)
                except json.JSONDecodeError as exc:
                    failures.append(f"{binary.relative_to(ROOT)} emitted invalid server info JSON: {exc}")
                else:
                    for field in ("name", "version", "minimum_supported_protocol_version", "runtime_scope"):
                        if field not in payload:
                            failures.append(f"{binary.relative_to(ROOT)} server info missing {field!r}")

        if verify_signatures:
            failures.extend(_verify_signature(binary, target))

    return failures, warnings


def _verify_signature(binary: Path, target: str) -> list[str]:
    if target.startswith("linux-"):
        return []
    if target.startswith("darwin-"):
        result = _run(["codesign", "--verify", "--strict", "--verbose=2", str(binary)])
        if result.returncode != 0:
            return [f"macOS signature verification failed for {binary.relative_to(ROOT)}: {result.stderr.strip()}"]
        return []
    if target.startswith("win32-"):
        if sys.platform != "win32":
            return [f"Windows signature verification must run on Windows for {binary.relative_to(ROOT)}"]
        result = _run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-AuthenticodeSignature '{binary}').Status",
            ]
        )
        if result.returncode != 0 or result.stdout.strip() != "Valid":
            return [f"Windows signature verification failed for {binary.relative_to(ROOT)}: {result.stdout.strip()}"]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and validate Codex plugin bundled Waggle runtimes.")
    parser.add_argument("--build-current", action="store_true", help="Build the current platform binary with PyInstaller.")
    parser.add_argument("--require-artifacts", action="store_true", help="Fail if any platform runtime binary is missing.")
    parser.add_argument("--probe", action="store_true", help="Run the --server-info startup probe for present artifacts.")
    parser.add_argument("--verify-signatures", action="store_true", help="Verify macOS and Windows code signatures.")
    parser.add_argument(
        "--allow-experimental-python",
        action="store_true",
        help="Allow runtime builds on Python versions outside the release-tested 3.11-3.13 range.",
    )
    args = parser.parse_args()

    if args.build_current:
        if sys.version_info < (3, 11) or sys.version_info >= (3, 14):
            if not args.allow_experimental_python:
                raise SystemExit(
                    "Codex plugin runtime builds are release-tested on Python 3.11-3.13. "
                    f"Current interpreter is {platform.python_version()}. "
                    "Use python3.11, python3.12, or python3.13, or pass --allow-experimental-python."
                )
        artifact = build_current()
        print(f"Built {artifact.relative_to(ROOT)}")

    failures, warnings = validate_layout(
        require_artifacts=args.require_artifacts,
        probe=args.probe,
        verify_signatures=args.verify_signatures,
    )
    for warning in warnings:
        print(f"Warning: {warning}")
    if failures:
        print("Codex plugin runtime validation failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("Codex plugin runtime validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
