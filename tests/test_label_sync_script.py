from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_github_labels.py"
SPEC = importlib.util.spec_from_file_location("sync_github_labels", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_parse_simple_yaml(tmp_path: Path) -> None:
    labels_file = tmp_path / "labels.yml"
    labels_file.write_text(
        """
- name: good first issue
  color: "7057ff"
  description: Small, well-scoped task suitable for first-time contributors.
- name: documentation
  color: "0075ca"
  description: Improvements to docs.
""".strip()
        + "\n",
        encoding="utf-8",
    )

    labels = MODULE.parse_simple_yaml(labels_file)

    assert [label.name for label in labels] == ["good first issue", "documentation"]
    assert labels[0].color == "7057ff"


def test_parse_simple_yaml_rejects_malformed_entry(tmp_path: Path) -> None:
    labels_file = tmp_path / "labels.yml"
    labels_file.write_text(
        """
- name: good first issue
  color: "7057ff"
  description: Small task.
- malformed entry without a colon
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"expected 'key: value' line, got 'malformed entry without a colon'"):
        MODULE.parse_simple_yaml(labels_file)


def test_sync_labels_plans_create_update_and_delete() -> None:
    desired = [
        MODULE.LabelDefinition("good first issue", "7057ff", "Small task."),
        MODULE.LabelDefinition("documentation", "0075ca", "Docs work."),
    ]
    existing = {
        "good first issue": {"name": "good first issue", "color": "7057ff", "description": "Old text."},
        "bug": {"name": "bug", "color": "d73a4a", "description": "Bug"},
    }

    class StubClient:
        def create_label(self, label):  # pragma: no cover - should not be called in dry-run
            raise AssertionError

        def update_label(self, existing_name, label):  # pragma: no cover - should not be called in dry-run
            raise AssertionError

        def delete_label(self, existing_name):  # pragma: no cover - should not be called in dry-run
            raise AssertionError

    actions = MODULE.sync_labels(
        desired,
        existing,
        client=StubClient(),
        dry_run=True,
        delete_missing=True,
    )

    assert actions == [
        "update:good first issue",
        "create:documentation",
        "delete:bug",
    ]


def test_main_requires_repository_and_token(tmp_path: Path, monkeypatch) -> None:
    labels_file = tmp_path / "labels.yml"
    labels_file.write_text(
        """
- name: good first issue
  color: "7057ff"
  description: Small task.
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    exit_code = MODULE.main(["--labels-file", str(labels_file)])

    assert exit_code == 2
