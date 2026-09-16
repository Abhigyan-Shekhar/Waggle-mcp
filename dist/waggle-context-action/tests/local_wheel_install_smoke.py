#!/usr/bin/env python3
"""Prove the Action install path with a locally built wheel, not live PyPI."""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
import tempfile
import venv
import zipfile
from email.parser import BytesParser
from pathlib import Path

PROOF_LABEL = "proves the install path works, not that this exact version is live on PyPI"
ACTION_ROOT = Path(__file__).parents[1]


def run(argv: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(argv, env=env, check=False, capture_output=True, text=True)
    if completed.returncode:
        command = " ".join(Path(argv[0]).name if index == 0 else part for index, part in enumerate(argv))
        raise RuntimeError(f"command failed ({command}):\n{completed.stdout}\n{completed.stderr}")
    return completed


def wheel_version(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        metadata_paths = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_paths) != 1:
            raise RuntimeError("wheel must contain exactly one METADATA file")
        metadata = BytesParser().parsebytes(archive.read(metadata_paths[0]))
    version = metadata.get("Version", "").strip()
    if not version:
        raise RuntimeError("wheel metadata has no Version")
    return version


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} WHEEL")
    wheel = Path(sys.argv[1]).resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise SystemExit("WHEEL must be one locally built wheel file")

    with tempfile.TemporaryDirectory(prefix="waggle-action-wheel-smoke-") as temporary:
        workspace = Path(temporary)
        environment_dir = workspace / "venv"
        venv.EnvBuilder(with_pip=True).create(environment_dir)
        python = environment_dir / "bin" / "python"
        fresh_site_packages = Path(
            run([str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"]).stdout.strip()
        )
        # Reuse only the CI job's dependency files. Nested .pth files are not
        # processed, so the parent's editable Waggle install is not inherited.
        (fresh_site_packages / "waggle-smoke-dependencies.pth").write_text(
            f"{sysconfig.get_paths()['purelib']}\n",
            encoding="utf-8",
        )

        event_path = ACTION_ROOT / "fixtures" / "issue.json"
        output_path = workspace / "github-output"
        summary_path = workspace / "step-summary"
        environment = dict(os.environ)
        environment.update(
            {
                "PATH": f"{environment_dir / 'bin'}{os.pathsep}{environment.get('PATH', '')}",
                "PIP_FIND_LINKS": str(wheel.parent),
                "PIP_IGNORE_INSTALLED": "1",
                "PIP_NO_DEPS": "1",
                "PIP_NO_INDEX": "1",
                "GITHUB_WORKSPACE": str(workspace),
                "GITHUB_REPOSITORY": "fixture/repository",
                "GITHUB_EVENT_NAME": "issues",
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_OUTPUT": str(output_path),
                "GITHUB_STEP_SUMMARY": str(summary_path),
                "WAGGLE_ACTION_EVENT_PATH": str(event_path),
                "WAGGLE_ACTION_CHECKPOINT": "",
                "WAGGLE_ACTION_SCOPE": "fixture/repository",
                "WAGGLE_ACTION_OUTPUT_DIRECTORY": ".waggle-output",
                "WAGGLE_ACTION_WAGGLE_VERSION": wheel_version(wheel),
                "WAGGLE_ACTION_UPLOAD_ARTIFACT": "false",
                "WAGGLE_ACTION_WRITE_STEP_SUMMARY": "true",
            }
        )

        try:
            completed = run([str(python), str(ACTION_ROOT / "scripts" / "run_action.py")], env=environment)
        except RuntimeError as action_error:
            diagnostic_environment = dict(environment)
            diagnostic_environment.update(
                {
                    "WAGGLE_BACKEND": "sqlite",
                    "WAGGLE_MODEL": "deterministic",
                    "WAGGLE_DB_PATH": str(workspace / "diagnostic.db"),
                }
            )
            diagnostic = subprocess.run(
                [
                    str(environment_dir / "bin" / "waggle-mcp"),
                    "ingest-github-event",
                    "--event-path",
                    str(event_path),
                    "--repository",
                    "fixture/repository",
                    "--project",
                    "fixture/repository",
                    "--scope",
                    "project",
                    "--output-context",
                    str(workspace / "diagnostic-context.md"),
                    "--output-checkpoint",
                    str(workspace / "diagnostic-memory.abhi"),
                ],
                env=diagnostic_environment,
                check=False,
                capture_output=True,
                text=True,
            )
            raise RuntimeError(
                f"{action_error}\ndirect CLI diagnostic:\n{diagnostic.stdout}\n{diagnostic.stderr}"
            ) from None
        context = workspace / ".waggle-output" / "context.md"
        checkpoint = workspace / ".waggle-output" / "memory.abhi"
        if not context.stat().st_size or not checkpoint.stat().st_size:
            raise RuntimeError("Action did not produce non-empty handoff files")
        if "status=ingested" not in completed.stdout:
            raise RuntimeError("Action did not report a successful ingestion")
        installed = run(
            [
                str(python),
                "-c",
                "import pathlib, waggle; print(pathlib.Path(waggle.__file__).resolve())",
            ],
            env=environment,
        )
        if not Path(installed.stdout.strip()).is_relative_to(environment_dir.resolve()):
            raise RuntimeError("Waggle did not load from the fresh environment's local wheel install")

    print(f"Local-wheel Action install smoke passed: {PROOF_LABEL}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
