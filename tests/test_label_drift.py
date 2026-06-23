from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Load check_label_refs script dynamically
MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_label_refs.py"
SPEC = importlib.util.spec_from_file_location("check_label_refs", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_extract_pattern_a() -> None:
    # Pattern A: Suggested / Recommended labels
    line1 = "- Suggested labels: `good first issue`, `help wanted`, `documentation`"
    assert MODULE._extract_label_refs_from_line(line1) == ["good first issue", "help wanted", "documentation"]

    line2 = "- Recommended labels: `bug`, `testing`"
    assert MODULE._extract_label_refs_from_line(line2) == ["bug", "testing"]


def test_extract_pattern_b() -> None:
    # Pattern B: Use `x` or such as `x` or start as `x`
    line1 = "Use `good first issue` for small tasks."
    assert MODULE._extract_label_refs_from_line(line1) == ["good first issue"]

    line2 = "Add one domain label such as `graph` or `retrieval`."
    assert MODULE._extract_label_refs_from_line(line2) == ["graph", "retrieval"]

    line3 = "New issues should usually start as `needs-triage` until approved."
    assert MODULE._extract_label_refs_from_line(line3) == ["needs-triage"]


def test_extract_pattern_c() -> None:
    # Pattern C: label such as / label: `x`
    line1 = "Every accepted PR should receive one difficulty label such as `level:beginner`."
    assert MODULE._extract_label_refs_from_line(line1) == ["level:beginner"]

    line2 = "one type label such as `type:docs` and a validation label: `gssoc:approved`"
    assert MODULE._extract_label_refs_from_line(line2) == ["type:docs", "gssoc:approved"]


def test_code_token_exclusions() -> None:
    # CLI flags
    assert MODULE._extract_label_refs_from_line("Use `--dry-run` to preview changes.") == []

    # Env variables
    assert MODULE._extract_label_refs_from_line("Ensure `WAGGLE_MODEL=deterministic` is set.") == []
    assert MODULE._extract_label_refs_from_line("Configure `WAGGLE_EMBEDDING_BACKEND` appropriately.") == []

    # Snake case / functions / paths
    assert MODULE._extract_label_refs_from_line("Use `tmp_path` fixture for testing.") == []
    assert MODULE._extract_label_refs_from_line("Verify with `agent_id` parameter.") == []
    assert MODULE._extract_label_refs_from_line("The file resides in `src/waggle/models.py` path.") == []

    # Multi-word commands
    assert MODULE._extract_label_refs_from_line("Run `waggle-mcp fsck <file.abhi>` to validate.") == []


def test_load_catalog(tmp_path: Path) -> None:
    catalog_file = tmp_path / "labels.yml"
    catalog_file.write_text(
        """
- name: good first issue
  color: "7057ff"
- name: "help wanted"
  color: "008672"
- name: 'documentation'
  color: "0075ca"
""",
        encoding="utf-8",
    )
    catalog = MODULE.load_catalog(catalog_file)
    assert catalog == {"good first issue", "help wanted", "documentation"}


def test_scan_doc(tmp_path: Path) -> None:
    doc_file = tmp_path / "test_doc.md"
    doc_file.write_text(
        """
# Testing Label Check
Use `good first issue` for starter tasks.
Also use `missing-label` which is not in the catalog.
Ensure `tmp_path` is not caught as a mismatch.
""",
        encoding="utf-8",
    )
    catalog = {"good first issue", "help wanted"}
    mismatches = MODULE.scan_doc(doc_file, catalog)
    assert mismatches == [(4, "missing-label")]


def test_main_success(tmp_path: Path) -> None:
    catalog_file = tmp_path / "labels.yml"
    catalog_file.write_text(
        """
- name: good first issue
- name: help wanted
""",
        encoding="utf-8",
    )
    doc_file = tmp_path / "test_doc.md"
    doc_file.write_text(
        """
Use `good first issue`.
""",
        encoding="utf-8",
    )

    exit_code = MODULE.main(["--catalog", str(catalog_file), "--docs", str(doc_file)])
    assert exit_code == 0


def test_main_mismatch(tmp_path: Path) -> None:
    catalog_file = tmp_path / "labels.yml"
    catalog_file.write_text(
        """
- name: good first issue
""",
        encoding="utf-8",
    )
    doc_file = tmp_path / "test_doc.md"
    doc_file.write_text(
        """
Use `help wanted`.
""",
        encoding="utf-8",
    )

    exit_code = MODULE.main(["--catalog", str(catalog_file), "--docs", str(doc_file)])
    assert exit_code == 1
