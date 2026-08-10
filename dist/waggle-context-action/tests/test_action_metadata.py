from __future__ import annotations

import re
from pathlib import Path

import yaml

ACTION_ROOT = Path(__file__).parents[1]


def load_action() -> dict[str, object]:
    document = yaml.safe_load((ACTION_ROOT / "action.yml").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_action_metadata_and_branding() -> None:
    action = load_action()
    assert action["name"] == "Waggle Context Handoff"
    assert action["branding"] == {"icon": "share-2", "color": "purple"}
    runs = action["runs"]
    assert isinstance(runs, dict)
    assert runs["using"] == "composite"


def test_inputs_match_the_public_contract() -> None:
    action = load_action()
    inputs = action["inputs"]
    assert isinstance(inputs, dict)
    assert set(inputs) == {
        "event-path",
        "checkpoint",
        "scope",
        "output-directory",
        "waggle-version",
        "upload-artifact",
        "write-step-summary",
    }
    defaults = {name: details.get("default") for name, details in inputs.items()}
    assert defaults == {
        "event-path": "",
        "checkpoint": "",
        "scope": "",
        "output-directory": ".waggle-output",
        "waggle-version": "0.1.25",
        "upload-artifact": "true",
        "write-step-summary": "true",
    }


def test_outputs_are_mapped_from_the_runner_step() -> None:
    outputs = load_action()["outputs"]
    assert isinstance(outputs, dict)
    assert set(outputs) == {"context-file", "checkpoint-file", "nodes-added", "edges-added"}
    for name, details in outputs.items():
        assert details["value"] == f"${{{{ steps.run.outputs.{name} }}}}"


def test_runner_is_fixed_and_artifact_action_is_sha_pinned() -> None:
    runs = load_action()["runs"]
    steps = runs["steps"]
    runner = next(step for step in steps if step.get("id") == "run")
    assert runner["shell"] == "bash"
    assert runner["run"] == 'python3 "$GITHUB_ACTION_PATH/scripts/run_action.py"'
    artifact = next(step for step in steps if "uses" in step)
    owner_action, sha = artifact["uses"].split("@", maxsplit=1)
    assert owner_action == "actions/upload-artifact"
    assert re.fullmatch(r"[0-9a-f]{40}", sha)
    assert artifact["if"] == "steps.run.outputs.upload-artifact == 'true'"
    assert artifact["with"]["path"].rstrip("\n") == (
        "${{ steps.run.outputs.context-file }}\n${{ steps.run.outputs.checkpoint-file }}"
    )
