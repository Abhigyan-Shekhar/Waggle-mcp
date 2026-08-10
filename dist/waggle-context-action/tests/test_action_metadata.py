from __future__ import annotations

import re
from pathlib import Path

import yaml

ACTION_ROOT = Path(__file__).parents[1]
PURPOSE = (
    "convert a GitHub issue, PR, discussion, release, or manually supplied workflow context into a portable "
    "Waggle memory checkpoint and a compact Markdown context handoff for downstream AI workflows."
)


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


def test_distribution_contains_required_documentation_and_examples() -> None:
    required = {
        "README.md",
        "LICENSE",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
        "RELEASE.md",
        "MARKETPLACE_RELEASE_CHECKLIST.md",
        "examples/issue-to-agent.yml",
        "examples/restore-checkpoint.yml",
    }
    assert {path for path in required if not (ACTION_ROOT / path).is_file()} == set()


def test_readme_states_the_exact_purpose_and_non_mutation_contract() -> None:
    readme = (ACTION_ROOT / "README.md").read_text(encoding="utf-8")
    assert PURPOSE in readme
    normalized = readme.lower()
    for statement in (
        "does not commit",
        "does not comment",
        "does not require a waggle-hosted account",
        "does not call an external llm",
    ):
        assert statement in normalized


def test_examples_use_read_only_permissions_and_pin_remote_actions() -> None:
    for example in sorted((ACTION_ROOT / "examples").glob("*.yml")):
        text = example.read_text(encoding="utf-8")
        assert "pull_request_target" not in text
        document = yaml.safe_load(text)
        assert document["permissions"] == {"contents": "read"}
        for job in document["jobs"].values():
            for step in job.get("steps", []):
                reference = step.get("uses", "")
                if reference and not reference.startswith("./"):
                    assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference)


def test_distribution_never_configures_pull_request_target() -> None:
    for path in ACTION_ROOT.rglob("*"):
        if path.is_file() and path.suffix in {".yml", ".yaml"}:
            assert "pull_request_target" not in path.read_text(encoding="utf-8")
    security = (ACTION_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "Do not use `pull_request_target`" in security


def test_local_documentation_links_resolve() -> None:
    for document in ACTION_ROOT.glob("*.md"):
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
            if "://" not in target and not target.startswith("#"):
                assert (document.parent / target.split("#", maxsplit=1)[0]).exists(), f"broken link in {document}: {target}"
