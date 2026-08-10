from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_action as runner  # noqa: E402

SECRET = "ghp_fixtureSecretMustNeverLeak123456789"


def base_environment(tmp_path: Path) -> dict[str, str]:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"body": f"hello; $(touch nope)\n{SECRET}"}), encoding="utf-8")
    return {
        "WAGGLE_ACTION_EVENT_PATH": str(event),
        "WAGGLE_ACTION_CHECKPOINT": "",
        "WAGGLE_ACTION_SCOPE": "octo/demo",
        "WAGGLE_ACTION_OUTPUT_DIRECTORY": ".waggle-output",
        "WAGGLE_ACTION_WAGGLE_VERSION": "0.1.25",
        "WAGGLE_ACTION_UPLOAD_ARTIFACT": "true",
        "WAGGLE_ACTION_WRITE_STEP_SUMMARY": "true",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_EVENT_NAME": "issues",
        "GITHUB_REPOSITORY": "octo/demo",
        "GITHUB_WORKSPACE": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "step-summary"),
    }


@pytest.mark.parametrize("key", ["WAGGLE_ACTION_UPLOAD_ARTIFACT", "WAGGLE_ACTION_WRITE_STEP_SUMMARY"])
def test_inputs_reject_non_boolean_values(tmp_path: Path, key: str) -> None:
    environment = base_environment(tmp_path)
    environment[key] = "yes"

    with pytest.raises(runner.ActionInputError, match="true or false"):
        runner.ActionInputs.from_environment(environment)


@pytest.mark.parametrize(
    "version",
    ["latest", "~=0.1", "0.1.25 --extra-index-url https://attacker", "git+https://example.test/repo", "01.1.1"],
)
def test_inputs_require_an_exact_normalized_version(tmp_path: Path, version: str) -> None:
    environment = base_environment(tmp_path)
    environment["WAGGLE_ACTION_WAGGLE_VERSION"] = version

    with pytest.raises(runner.ActionInputError, match="exact normalized version"):
        runner.ActionInputs.from_environment(environment)


def test_inputs_fall_back_to_github_runtime_values(tmp_path: Path) -> None:
    environment = base_environment(tmp_path)
    environment["WAGGLE_ACTION_EVENT_PATH"] = ""
    environment["WAGGLE_ACTION_SCOPE"] = ""

    inputs = runner.ActionInputs.from_environment(environment)

    assert inputs.event_path == Path(environment["GITHUB_EVENT_PATH"]).resolve()
    assert inputs.project == "octo/demo"
    assert inputs.repository == "octo/demo"


@pytest.mark.parametrize("output", ["../escape", "/tmp/outside"])
def test_inputs_reject_output_directories_outside_workspace(tmp_path: Path, output: str) -> None:
    environment = base_environment(tmp_path)
    environment["WAGGLE_ACTION_OUTPUT_DIRECTORY"] = output

    with pytest.raises(runner.ActionInputError, match="inside GITHUB_WORKSPACE"):
        runner.ActionInputs.from_environment(environment)


def test_inputs_reject_output_symlink_that_escapes_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "linked-output").symlink_to(outside, target_is_directory=True)
    environment = base_environment(tmp_path)
    environment["WAGGLE_ACTION_OUTPUT_DIRECTORY"] = "linked-output"

    with pytest.raises(runner.ActionInputError, match="inside GITHUB_WORKSPACE"):
        runner.ActionInputs.from_environment(environment)


def test_run_checked_never_uses_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed.update(argv=argv, kwargs=kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.run_checked(["waggle-mcp", "--help"], env={})

    assert result.returncode == 0
    assert observed["argv"] == ["waggle-mcp", "--help"]
    assert observed["kwargs"]["shell"] is False
    assert observed["kwargs"]["check"] is True
    assert observed["kwargs"]["capture_output"] is True


def test_run_checked_sanitizes_subprocess_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, ["waggle-mcp"], output=SECRET, stderr=SECRET)

    monkeypatch.setattr(runner.subprocess, "run", fail)

    with pytest.raises(runner.ActionExecutionError) as raised:
        runner.run_checked(["waggle-mcp", "ingest-github-event"], env={})

    assert SECRET not in str(raised.value)
    assert "waggle-mcp" in str(raised.value)


def test_action_imports_checkpoint_and_maps_scope_to_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = base_environment(tmp_path)
    checkpoint = tmp_path / "prior.abhi"
    checkpoint.write_text("prior", encoding="utf-8")
    environment["WAGGLE_ACTION_CHECKPOINT"] = str(checkpoint)
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command_environment = kwargs["env"]
        calls.append((argv, command_environment))
        if "ingest-github-event" in argv:
            context = Path(argv[argv.index("--output-context") + 1])
            exported = Path(argv[argv.index("--output-checkpoint") + 1])
            context.write_text("# Safe handoff\n", encoding="utf-8")
            exported.write_text("checkpoint", encoding="utf-8")
            result = {
                "status": "ingested",
                "event_type": "issue",
                "repository": "octo/demo",
                "project": "octo/demo",
                "context_file": str(context),
                "checkpoint_file": str(exported),
                "nodes_added": 3,
                "edges_added": 2,
            }
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(result), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.run_action(runner.ActionInputs.from_environment(environment), environ=environment)

    assert calls[0][0][:4] == [sys.executable, "-m", "pip", "install"]
    assert calls[0][0][-1] == "waggle-mcp==0.1.25"
    assert calls[1][0] == ["waggle-mcp", "import", "--input", str(checkpoint.resolve()), "--reembed-on-mismatch"]
    ingest_argv = calls[2][0]
    assert ingest_argv[ingest_argv.index("--project") + 1] == "octo/demo"
    assert ingest_argv[ingest_argv.index("--scope") + 1] == "project"
    assert "--session-id" not in ingest_argv
    assert SECRET not in " ".join(" ".join(argv) for argv, _ in calls)
    assert calls[2][1]["WAGGLE_BACKEND"] == "sqlite"
    assert calls[2][1]["WAGGLE_MODEL"] == "deterministic"
    assert Path(calls[2][1]["WAGGLE_DB_PATH"]).parent != tmp_path
    assert not Path(calls[2][1]["WAGGLE_DB_PATH"]).parent.exists()
    assert result.nodes_added == 3
    assert result.edges_added == 2
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err

    output_text = Path(environment["GITHUB_OUTPUT"]).read_text(encoding="utf-8")
    assert "context-file<<waggle_" in output_text
    assert "checkpoint-file<<waggle_" in output_text
    assert "nodes-added<<waggle_" in output_text
    assert SECRET not in output_text
    summary = Path(environment["GITHUB_STEP_SUMMARY"]).read_text(encoding="utf-8")
    assert "3" in summary and "2" in summary
    assert SECRET not in summary


def test_action_can_disable_step_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environment = base_environment(tmp_path)
    environment["WAGGLE_ACTION_WRITE_STEP_SUMMARY"] = "false"

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "ingest-github-event" not in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")
        context = Path(argv[argv.index("--output-context") + 1])
        checkpoint = Path(argv[argv.index("--output-checkpoint") + 1])
        context.write_text("safe", encoding="utf-8")
        checkpoint.write_text("safe", encoding="utf-8")
        payload = {
            "status": "unsupported",
            "event_type": "unknown",
            "repository": "octo/demo",
            "project": "octo/demo",
            "context_file": str(context),
            "checkpoint_file": str(checkpoint),
            "nodes_added": 0,
            "edges_added": 0,
        }
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner.run_action(runner.ActionInputs.from_environment(environment), environ=environment)

    assert not Path(environment["GITHUB_STEP_SUMMARY"]).exists()

