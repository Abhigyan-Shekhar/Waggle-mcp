#!/usr/bin/env python3
"""Trusted runner for the Waggle Context Handoff composite action."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_EXACT_VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:(?:a|b|rc)(?:0|[1-9]\d*))?")


class ActionInputError(ValueError):
    """Raised when action metadata inputs are unsafe or incomplete."""


class ActionExecutionError(RuntimeError):
    """Raised when a child process fails without exposing captured data."""


def _boolean(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise ActionInputError(f"{name} must be true or false.")
    return normalized == "true"


def _required_path(value: str, *, name: str) -> Path:
    if not value.strip():
        raise ActionInputError(f"{name} is required.")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ActionInputError(f"{name} must reference an existing file.")
    return path


def _optional_path(value: str, *, name: str) -> Path | None:
    return _required_path(value, name=name) if value.strip() else None


def _output_path(value: str, *, workspace: Path) -> Path:
    if not value.strip():
        raise ActionInputError("output-directory is required.")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    if resolved == workspace or not resolved.is_relative_to(workspace):
        raise ActionInputError("output-directory must resolve inside GITHUB_WORKSPACE.")
    return resolved


@dataclass(frozen=True)
class ActionInputs:
    event_path: Path
    checkpoint: Path | None
    project: str
    repository: str
    event_name: str
    output_directory: Path
    waggle_version: str
    upload_artifact: bool
    write_step_summary: bool

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> ActionInputs:
        workspace_raw = env.get("GITHUB_WORKSPACE", "").strip()
        if not workspace_raw:
            raise ActionInputError("GITHUB_WORKSPACE is required.")
        workspace = Path(workspace_raw).expanduser().resolve()
        repository = env.get("GITHUB_REPOSITORY", "").strip()
        if not repository:
            raise ActionInputError("GITHUB_REPOSITORY is required.")
        event_path = env.get("WAGGLE_ACTION_EVENT_PATH", "").strip() or env.get("GITHUB_EVENT_PATH", "").strip()
        project = env.get("WAGGLE_ACTION_SCOPE", "").strip() or repository
        version = env.get("WAGGLE_ACTION_WAGGLE_VERSION", "").strip()
        if _EXACT_VERSION.fullmatch(version) is None:
            raise ActionInputError("waggle-version must be an exact normalized version such as 0.1.25.")
        return cls(
            event_path=_required_path(event_path, name="event-path"),
            checkpoint=_optional_path(env.get("WAGGLE_ACTION_CHECKPOINT", ""), name="checkpoint"),
            project=project,
            repository=repository,
            event_name=env.get("GITHUB_EVENT_NAME", "").strip(),
            output_directory=_output_path(
                env.get("WAGGLE_ACTION_OUTPUT_DIRECTORY", ".waggle-output"),
                workspace=workspace,
            ),
            waggle_version=version,
            upload_artifact=_boolean(
                env.get("WAGGLE_ACTION_UPLOAD_ARTIFACT", "true"),
                name="upload-artifact",
            ),
            write_step_summary=_boolean(
                env.get("WAGGLE_ACTION_WRITE_STEP_SUMMARY", "true"),
                name="write-step-summary",
            ),
        )


@dataclass(frozen=True)
class ActionResult:
    status: str
    event_type: str
    repository: str
    project: str
    context_file: Path
    checkpoint_file: Path
    nodes_added: int
    edges_added: int


def run_checked(argv: Sequence[str], *, env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    command = [str(part) for part in argv]
    try:
        return subprocess.run(
            command,
            env=dict(env),
            shell=False,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        executable = Path(command[0]).name if command else "child process"
        raise ActionExecutionError(
            f"{executable} failed with exit code {exc.returncode}; captured output was suppressed."
        ) from None


def _parse_result(stdout: str) -> ActionResult:
    try:
        payload: Any = json.loads(stdout)
        if not isinstance(payload, dict):
            raise TypeError
        return ActionResult(
            status=str(payload["status"]),
            event_type=str(payload["event_type"]),
            repository=str(payload["repository"]),
            project=str(payload["project"]),
            context_file=Path(str(payload["context_file"])).resolve(),
            checkpoint_file=Path(str(payload["checkpoint_file"])).resolve(),
            nodes_added=int(payload["nodes_added"]),
            edges_added=int(payload["edges_added"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ActionExecutionError("waggle-mcp returned an invalid result; captured output was suppressed.") from None


def _append_output(path: Path, name: str, value: str) -> None:
    delimiter = f"waggle_{uuid.uuid4().hex}"
    while delimiter in value:
        delimiter = f"waggle_{uuid.uuid4().hex}"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


def _summary_value(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").replace("|", "\\|").replace("`", "'")


def _write_summary(path: Path, result: ActionResult) -> None:
    rows = (
        ("Status", result.status),
        ("Event type", result.event_type),
        ("Repository", result.repository),
        ("Project", result.project),
        ("Context", str(result.context_file)),
        ("Checkpoint", str(result.checkpoint_file)),
        ("Nodes added", str(result.nodes_added)),
        ("Edges added", str(result.edges_added)),
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("## Waggle Context Handoff\n\n| Field | Value |\n| --- | --- |\n")
        for name, value in rows:
            handle.write(f"| {name} | `{_summary_value(value)}` |\n")
        handle.write("\n")


def run_action(inputs: ActionInputs, *, environ: MutableMapping[str, str]) -> ActionResult:
    inputs.output_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    context_file = inputs.output_directory / "context.md"
    checkpoint_file = inputs.output_directory / "memory.abhi"

    command_environment = dict(environ)
    command_environment.update({"WAGGLE_BACKEND": "sqlite", "WAGGLE_MODEL": "deterministic"})
    if command_environment.get("WAGGLE_ACTION_SKIP_INSTALL", "").lower() != "true":
        run_checked(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                f"waggle-mcp=={inputs.waggle_version}",
            ],
            env=command_environment,
        )

    with tempfile.TemporaryDirectory(prefix="waggle-context-action-") as temporary_directory:
        command_environment["WAGGLE_DB_PATH"] = str(Path(temporary_directory) / "memory.db")
        if inputs.checkpoint is not None:
            run_checked(
                ["waggle-mcp", "import", "--input", str(inputs.checkpoint), "--reembed-on-mismatch"],
                env=command_environment,
            )
        completed = run_checked(
            [
                "waggle-mcp",
                "ingest-github-event",
                "--event-path",
                str(inputs.event_path),
                "--repository",
                inputs.repository,
                "--project",
                inputs.project,
                "--scope",
                "project",
                "--output-context",
                str(context_file),
                "--output-checkpoint",
                str(checkpoint_file),
            ],
            env=command_environment,
        )
        result = _parse_result(completed.stdout)

    if result.context_file != context_file.resolve() or result.checkpoint_file != checkpoint_file.resolve():
        raise ActionExecutionError("waggle-mcp returned output paths outside the requested output directory.")
    if not result.context_file.is_file() or not result.checkpoint_file.is_file():
        raise ActionExecutionError("waggle-mcp did not create the declared output files.")

    github_output = environ.get("GITHUB_OUTPUT", "").strip()
    if github_output:
        output_path = Path(github_output)
        _append_output(output_path, "context-file", str(result.context_file))
        _append_output(output_path, "checkpoint-file", str(result.checkpoint_file))
        _append_output(output_path, "nodes-added", str(result.nodes_added))
        _append_output(output_path, "edges-added", str(result.edges_added))
        _append_output(output_path, "upload-artifact", str(inputs.upload_artifact).lower())

    summary_path = environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if inputs.write_step_summary and summary_path:
        _write_summary(Path(summary_path), result)
    return result


def main() -> int:
    try:
        result = run_action(ActionInputs.from_environment(os.environ), environ=os.environ)
    except (ActionInputError, ActionExecutionError) as exc:
        print(f"Waggle Context Handoff failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Waggle Context Handoff: status={_summary_value(result.status)} "
        f"nodes-added={result.nodes_added} edges-added={result.edges_added}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
