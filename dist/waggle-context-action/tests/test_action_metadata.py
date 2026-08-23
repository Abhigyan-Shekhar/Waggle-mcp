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
    waggle_version = inputs["waggle-version"]
    assert waggle_version["required"] is True
    assert "default" not in waggle_version
    defaults = {name: details.get("default") for name, details in inputs.items() if name != "waggle-version"}
    assert defaults == {
        "event-path": "",
        "checkpoint": "",
        "scope": "",
        "output-directory": ".waggle-output",
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


def test_readme_explains_why_waggle_version_has_no_default() -> None:
    readme = (ACTION_ROOT / "README.md").read_text(encoding="utf-8").lower()
    assert (
        "no published release currently contains `ingest-github-event`; specify the version explicitly once one exists."
    ) in readme


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
                assert (document.parent / target.split("#", maxsplit=1)[0]).exists(), (
                    f"broken link in {document}: {target}"
                )


def test_dependabot_covers_python_and_github_actions() -> None:
    path = ACTION_ROOT / ".github" / "dependabot.yml"
    assert path.is_file()
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    ecosystems = {update["package-ecosystem"] for update in document["updates"]}
    assert ecosystems == {"pip", "github-actions"}
    assert all(update["directory"] == "/" for update in document["updates"])


def test_ci_is_read_only_sha_pinned_and_runs_all_required_checks() -> None:
    ci_path = ACTION_ROOT / ".github" / "workflows" / "ci.yml"
    fixture_path = ACTION_ROOT / ".github" / "workflows" / "fixture-example.yml"
    assert ci_path.is_file() and fixture_path.is_file()
    for path in (ci_path, fixture_path):
        text = path.read_text(encoding="utf-8")
        document = yaml.safe_load(text)
        assert document["permissions"] == {"contents": "read"}
        assert "write" not in text
        for reference in re.findall(r"uses:\s*([^\s#]+)", text):
            if not reference.startswith("./"):
                assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference)

    ci = ci_path.read_text(encoding="utf-8")
    for command in ("pytest", "ruff check", "mypy", "yamllint", "test_integration.py"):
        assert command in ci
    fixture = fixture_path.read_text(encoding="utf-8")
    assert "uses: ./" in fixture
    assert "fixtures/issue.json" in fixture
    assert "test -s" in fixture

    repository_root = ACTION_ROOT.parents[1]
    root_ci = (repository_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "Local-wheel Action install smoke" in root_ci
    assert "proves the install path works, not that this exact version is live on PyPI" in root_ci
