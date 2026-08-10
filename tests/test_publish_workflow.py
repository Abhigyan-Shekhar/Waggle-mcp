from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "publish-python-and-mcp.yml"
PUBLISH_CONDITION_PARTS = (
    "github.event_name == 'push'",
    "github.ref_type == 'tag'",
    "startsWith(github.ref_name, 'v')",
)
ACTION_SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def load_workflow() -> dict[str, Any]:
    payload = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(payload, dict)
    return payload


def job_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    return steps


def run_commands(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in job_steps(job) if "run" in step]


def test_workflow_has_only_manual_validation_and_version_tag_triggers() -> None:
    workflow = load_workflow()

    assert set(workflow["on"]) == {"workflow_dispatch", "push"}
    assert workflow["on"]["push"]["tags"] == ["v*"]
    assert workflow["concurrency"] == {
        "group": "release-${{ github.ref }}",
        "cancel-in-progress": "false",
    }


def test_workflow_has_exact_ordered_job_chain() -> None:
    jobs = load_workflow()["jobs"]

    assert set(jobs) == {"validate-build", "pypi-publish", "mcp-registry-publish", "release-assets"}
    assert "needs" not in jobs["validate-build"]
    assert jobs["pypi-publish"]["needs"] == "validate-build"
    assert jobs["mcp-registry-publish"]["needs"] == "pypi-publish"
    assert jobs["release-assets"]["needs"] == "mcp-registry-publish"


def test_validated_version_flows_through_each_direct_dependency() -> None:
    jobs = load_workflow()["jobs"]

    assert jobs["pypi-publish"]["outputs"] == {"version": "${{ needs.validate-build.outputs.version }}"}
    registry_commands = "\n".join(run_commands(jobs["mcp-registry-publish"]))
    assert "${{ needs.pypi-publish.outputs.version }}" in registry_commands
    assert "${{ needs.validate-build.outputs.version }}" not in registry_commands


def test_workflow_uses_least_privilege_and_both_environment_gates() -> None:
    workflow = load_workflow()
    jobs = workflow["jobs"]

    assert workflow["permissions"] == {}
    assert jobs["validate-build"]["permissions"] == {"contents": "read"}
    assert jobs["pypi-publish"]["permissions"] == {"id-token": "write"}
    assert jobs["mcp-registry-publish"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
    }
    assert jobs["release-assets"]["permissions"] == {"contents": "write"}
    assert jobs["pypi-publish"]["environment"] == "pypi"
    assert jobs["mcp-registry-publish"]["environment"] == "mcp-registry"


def test_all_remote_state_jobs_require_a_version_tag_push() -> None:
    jobs = load_workflow()["jobs"]

    for name in ("pypi-publish", "mcp-registry-publish", "release-assets"):
        condition = jobs[name]["if"]
        assert all(part in condition for part in PUBLISH_CONDITION_PARTS), name


def test_validation_builds_once_and_hands_off_original_artifacts() -> None:
    jobs = load_workflow()["jobs"]
    validate_commands = run_commands(jobs["validate-build"])
    later_commands = [
        command
        for name in ("pypi-publish", "mcp-registry-publish", "release-assets")
        for command in run_commands(jobs[name])
    ]

    assert sum("python -m build" in command for command in validate_commands) == 1
    assert not any("python -m build" in command for command in later_commands)
    assert any(step.get("with", {}).get("name") == "python-distributions" for step in job_steps(jobs["validate-build"]))
    assert any(step.get("with", {}).get("name") == "release-verifier" for step in job_steps(jobs["validate-build"]))


def test_registry_metadata_is_checked_not_rewritten_or_pushed() -> None:
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    registry_commands = run_commands(load_workflow()["jobs"]["mcp-registry-publish"])

    assert workflow_text.count("sync_release_metadata.py --check") == 2
    assert "sync_release_metadata.py --write" not in workflow_text
    assert "git commit" not in workflow_text
    assert "git push" not in workflow_text
    assert next(index for index, command in enumerate(registry_commands) if "--check" in command) < next(
        index for index, command in enumerate(registry_commands) if "login github-oidc" in command
    )


def test_pypi_job_has_no_checkout_build_or_long_lived_secret() -> None:
    job = load_workflow()["jobs"]["pypi-publish"]
    serialized = yaml.safe_dump(job)

    assert "actions/checkout" not in serialized
    assert "python -m build" not in serialized
    assert "password:" not in serialized
    assert "api-token" not in serialized
    assert "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33" in serialized


def test_registry_pin_checksum_immutability_warning_and_exact_verification_are_present() -> None:
    serialized = yaml.safe_dump(load_workflow()["jobs"]["mcp-registry-publish"])

    assert "v1.8.1/mcp-publisher_linux_amd64.tar.gz" in serialized
    assert "a06c9096dcb9727c13555b6be26c7effa707b01f06a4c561ba7a3635443cf2cc" in serialized
    assert "MCP Registry versions are immutable" in WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "io.github.Abhigyan-Shekhar/Waggle-mcp" in serialized
    assert "--package waggle-mcp" in serialized


def test_release_assets_reuses_or_creates_a_draft_without_publishing_it() -> None:
    commands = "\n".join(run_commands(load_workflow()["jobs"]["release-assets"]))

    assert 'gh release view "$GITHUB_REF_NAME"' in commands
    assert "gh release create" in commands
    assert "--draft" in commands
    assert "gh release upload" in commands
    assert "--clobber" in commands
    assert "--draft=false" not in commands


def test_all_third_party_actions_are_pinned_to_full_commit_shas() -> None:
    jobs = load_workflow()["jobs"]

    action_refs = [step["uses"] for job in jobs.values() for step in job_steps(job) if "uses" in step]
    assert action_refs
    assert all(ACTION_SHA.fullmatch(ref) for ref in action_refs)
