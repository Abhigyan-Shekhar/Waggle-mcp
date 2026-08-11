from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from waggle.abhi import load_abhi_document

ACTION_ROOT = Path(__file__).parents[1]
RUNNER = ACTION_ROOT / "scripts" / "run_action.py"
FIXTURES = ACTION_ROOT / "fixtures"
SECRET_MARKER = "FIXTURE_SECRET_DO_NOT_LEAK"

EVENT_CASES = (
    ("issue", "issues", 3, 2),
    ("pull_request", "pull_request", 3, 2),
    ("discussion", "discussion", 3, 2),
    ("release", "release", 3, 2),
    ("push", "push", 5, 4),
    ("generic", "workflow_dispatch", 2, 1),
)


def _write_ai_and_network_sentinel(root: Path, marker: Path) -> None:
    sentinel = f"""
import socket
from pathlib import Path

_marker = Path({str(marker)!r})
_original_socket = socket.socket

def _blocked(*args, **kwargs):
    _marker.write_text("network constructor reached", encoding="utf-8")
    raise RuntimeError("network disabled by integration sentinel")

class _BlockedSocket(_original_socket):
    def connect(self, *args, **kwargs):
        return _blocked(*args, **kwargs)

socket.create_connection = _blocked
socket.socket = _BlockedSocket
""".lstrip()
    (root / "sitecustomize.py").write_text(sentinel, encoding="utf-8")
    blocked_client = f"""
from pathlib import Path
_marker = Path({str(marker)!r})
class _BlockedClient:
    def __init__(self, *args, **kwargs):
        _marker.write_text("external AI constructor reached", encoding="utf-8")
        raise RuntimeError("external AI disabled by integration sentinel")
OpenAI = AsyncOpenAI = Anthropic = AsyncAnthropic = Client = _BlockedClient
""".lstrip()
    for package in ("openai", "anthropic", "google/genai", "google/generativeai"):
        package_path = root / package
        package_path.mkdir(parents=True)
        (package_path / "__init__.py").write_text(blocked_client, encoding="utf-8")


def _parse_github_outputs(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    outputs: dict[str, str] = {}
    index = 0
    while index < len(lines):
        name, delimiter = lines[index].split("<<", maxsplit=1)
        index += 1
        values: list[str] = []
        while index < len(lines) and lines[index] != delimiter:
            values.append(lines[index])
            index += 1
        if index == len(lines):
            raise AssertionError(f"unterminated GitHub output {name}")
        outputs[name] = "\n".join(values)
        index += 1
    return outputs


def _run(command: list[str], *, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        env=environment,
        shell=False,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(("fixture_name", "event_name", "expected_nodes", "expected_edges"), EVENT_CASES)
def test_real_action_runner_proves_secure_offline_handoff(
    tmp_path: Path,
    fixture_name: str,
    event_name: str,
    expected_nodes: int,
    expected_edges: int,
) -> None:
    sentinel_root = tmp_path / "sentinel"
    sentinel_root.mkdir()
    sentinel_marker = tmp_path / "external-call-attempted"
    _write_ai_and_network_sentinel(sentinel_root, sentinel_marker)

    github_output = tmp_path / "github-output"
    step_summary = tmp_path / "step-summary"
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{environment.get('PATH', '')}",
            "PYTHONPATH": f"{sentinel_root}{os.pathsep}{environment.get('PYTHONPATH', '')}",
            "GITHUB_EVENT_PATH": str(FIXTURES / f"{fixture_name}.json"),
            "GITHUB_EVENT_NAME": event_name,
            "GITHUB_REPOSITORY": "octo/demo",
            "GITHUB_WORKSPACE": str(tmp_path),
            "GITHUB_OUTPUT": str(github_output),
            "GITHUB_STEP_SUMMARY": str(step_summary),
            "WAGGLE_ACTION_EVENT_PATH": "",
            "WAGGLE_ACTION_CHECKPOINT": "",
            "WAGGLE_ACTION_SCOPE": "",
            "WAGGLE_ACTION_OUTPUT_DIRECTORY": ".waggle-output",
            "WAGGLE_ACTION_WAGGLE_VERSION": "1.2.3",
            "WAGGLE_ACTION_UPLOAD_ARTIFACT": "false",
            "WAGGLE_ACTION_WRITE_STEP_SUMMARY": "true",
            "WAGGLE_ACTION_SKIP_INSTALL": "true",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )

    action = _run([sys.executable, str(RUNNER)], environment=environment)
    outputs = _parse_github_outputs(github_output)
    context_file = Path(outputs["context-file"])
    checkpoint_file = Path(outputs["checkpoint-file"])
    assert context_file.is_file() and context_file.stat().st_size > 0
    assert checkpoint_file.is_file() and checkpoint_file.stat().st_size > 0
    assert int(outputs["nodes-added"]) == expected_nodes
    assert int(outputs["edges-added"]) == expected_edges
    assert not sentinel_marker.exists()

    verification_environment = dict(environment)
    verification_environment["WAGGLE_DB_PATH"] = str(tmp_path / "verification.db")
    verification_environment["WAGGLE_BACKEND"] = "sqlite"
    verification_environment["WAGGLE_MODEL"] = "deterministic"
    validation = _run(["waggle-mcp", "validate", "--input", str(checkpoint_file)], environment=verification_environment)
    inspection = _run(["waggle-mcp", "inspect", "--input", str(checkpoint_file)], environment=verification_environment)
    validation_result = json.loads(validation.stdout)
    inspection_result = json.loads(inspection.stdout)
    assert validation_result["valid"] is True
    assert validation_result["node_count"] >= expected_nodes
    assert validation_result["edge_count"] >= expected_edges
    assert inspection_result["node_count"] >= expected_nodes
    assert inspection_result["edge_count"] >= expected_edges

    inspection_directory = tmp_path / "bounded-checkpoint-inspection"
    inspection_directory.mkdir()
    document = load_abhi_document(checkpoint_file)
    for name in ("manifest", "transcripts", "nodes", "edges", "context_windows"):
        (inspection_directory / f"{name}.json").write_text(
            json.dumps(document[name], ensure_ascii=False, sort_keys=True, default=str),
            encoding="utf-8",
        )

    integration_log = tmp_path / "integration.log"
    integration_log.write_text(
        action.stdout + action.stderr + validation.stdout + validation.stderr + inspection.stdout + inspection.stderr,
        encoding="utf-8",
    )
    secret_proof = _run(
        [
            "bash",
            str(ACTION_ROOT / "tests" / "assert_no_secret.sh"),
            SECRET_MARKER,
            str(integration_log),
            str(context_file),
            str(checkpoint_file),
            str(github_output),
            str(step_summary),
            str(inspection_directory),
        ],
        environment=environment,
    )

    print(f"\n[{fixture_name}] action: {action.stdout.strip()}")
    print(
        f"[{fixture_name}] artifacts: checkpoint={checkpoint_file.stat().st_size} bytes; "
        f"context={context_file.stat().st_size} bytes"
    )
    print(f"[{fixture_name}] counts: nodes={outputs['nodes-added']}; edges={outputs['edges-added']}")
    print(
        f"[{fixture_name}] validation: valid={validation_result['valid']}; "
        f"nodes={validation_result['node_count']}; edges={validation_result['edge_count']}"
    )
    print(f"[{fixture_name}] AI/network sentinel: no constructor reached")
    print(secret_proof.stdout.strip())

    searched_output = "\n".join(
        [
            integration_log.read_text(encoding="utf-8"),
            context_file.read_text(encoding="utf-8"),
            github_output.read_text(encoding="utf-8"),
            step_summary.read_text(encoding="utf-8"),
            *(path.read_text(encoding="utf-8") for path in inspection_directory.iterdir()),
        ]
    )
    assert SECRET_MARKER not in searched_output
    assert "/tmp/waggle-owned" not in action.stdout + action.stderr
    assert not Path("/tmp/waggle-owned").exists()
