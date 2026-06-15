#!/usr/bin/env python3
"""
check_label_refs.py -- Label catalog drift checker.

Parses the label catalog defined in .github/labels.yml and scans a
configurable list of contributor-facing docs for label references.
Exits non-zero with a clear mismatch report when any referenced label
name is absent from the catalog.

Detection strategy
------------------
Rather than scanning all backtick tokens (which produces many false
positives from code snippets), this script uses targeted patterns that
match how labels are actually referenced in contributor docs:

  Pattern A -- "Suggested labels:" lines (good-first-issues.md style):
      - Suggested labels: `good first issue`, `help wanted`, `tooling`

  Pattern B -- "Use/Add ... label" sentences (CONTRIBUTING.md style):
      Use `good first issue` for small, well-scoped tasks.
      Add one domain label such as `graph`, `retrieval`, or `tooling`.
      New issues should usually start as `needs-triage` ...

  Pattern C -- "label(s):" inline list pattern:
      Every accepted program PR should receive one difficulty label such
      as `level:beginner`, one type label such as `type:docs`, and a
      validation label such as `gssoc:approved` ...

Any backtick-quoted string resolved by these patterns that is NOT in
the current catalog is reported as a mismatch.

Usage
-----
    # From the repository root:
    python3 scripts/check_label_refs.py

    # Explicit paths (override defaults):
    python3 scripts/check_label_refs.py \\
        --catalog .github/labels.yml \\
        --docs CONTRIBUTING.md docs/good-first-issues.md

    # Verbose: show all catalog labels before scanning
    python3 scripts/check_label_refs.py --verbose

Exit codes
----------
    0  All label references found in the catalog -- no drift.
    1  One or more label references are missing from the catalog.
    2  The catalog file could not be parsed (bad YAML / missing file).

Maintainer guide: keeping labels and docs in sync
--------------------------------------------------
When you rename or remove a label from .github/labels.yml:

  1. Search the scanned docs for the old label name:
       grep -rn "old-label-name" CONTRIBUTING.md docs/good-first-issues.md

  2. Replace every reference with the new name (or remove the reference
     if the label was deleted with no replacement).

  3. Run this script to confirm no drift remains:
       python3 scripts/check_label_refs.py

  4. Sync the live GitHub labels:
       python3 scripts/sync_github_labels.py --dry-run   # preview
       python3 scripts/sync_github_labels.py             # apply
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows (avoids cp1252 encode errors in CI)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Defaults -- paths relative to the repository root
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CATALOG = REPO_ROOT / ".github" / "labels.yml"

DEFAULT_DOCS = [
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "docs" / "good-first-issues.md",
]

# ---------------------------------------------------------------------------
# Label-reference detection patterns
# ---------------------------------------------------------------------------
# Each pattern captures one backtick-quoted token that is an intentional
# label reference. All are applied per line.

# Pattern A: "Suggested labels:" / "Recommended labels:" lines
# Captures every backtick token on that line after the prefix.
_SUGGESTED_RE = re.compile(
    r"(?i)(?:suggested|recommended)\s+labels?\s*:\s*(.*)",
)

# Pattern B: list context following "such as", "for example", "example:", "e.g.", or "like"
# Captures every backtick token on that line after the trigger keyword.
_LIST_CONTEXT_RE = re.compile(
    r"(?i)\b(?:such\s+as|for\s+example|example|e\.g\.|like)\b[,:]?\s*(.*)",
)

# Pattern C: specific use/add/start-as guidance (singular backtick tokens)
_SPECIFIC_LABEL_RE = re.compile(r"(?i)(?:use|add|start\s+as|start\s+with)\s+`([^`]+)`")

_CODE_TOKEN_RE = re.compile(
    r"^(?:"
    r"--[\w-]+"  # CLI flags: --dry-run
    r"|[A-Z][A-Z0-9_]+=\S+"  # env assignments: WAGGLE_MODEL=deterministic
    r"|[A-Z][A-Z0-9_]{2,}"  # ALL_CAPS env var names (3+ chars)
    r"|\w*_\w+"  # snake_case identifiers: tmp_path, agent_id
    r"|.*[()/{\\=<>].*"  # contains code chars
    r"|.*\d+\.\d+.*"  # version numbers
    r")$"
)

# Pattern D: bare comma-separated backtick list after "Suggested labels:" or
# on lines inside a "Suggested labels" block (covers multi-column list forms)
_BACKTICK_TOKEN_RE = re.compile(r"`([^`]+)`")


def _extract_label_refs_from_line(line: str) -> list[str]:
    """Return all label references found on a single doc line."""
    refs: list[str] = []

    # Pattern A -- "Suggested/Recommended labels: ..." -- extract ALL backtick tokens after it
    m = _SUGGESTED_RE.search(line)
    if m:
        for token in _BACKTICK_TOKEN_RE.findall(m.group(1)):
            if not _CODE_TOKEN_RE.match(token):
                refs.append(token)
        return list(dict.fromkeys(refs))

    # Pattern B -- list following such as / example / like -- extract ALL backtick tokens after it
    m = _LIST_CONTEXT_RE.search(line)
    if m:
        for token in _BACKTICK_TOKEN_RE.findall(m.group(1)):
            if not _CODE_TOKEN_RE.match(token):
                refs.append(token)
        return list(dict.fromkeys(refs))

    # Pattern C -- specific Use/Add/start as references
    for m in _SPECIFIC_LABEL_RE.finditer(line):
        token = m.group(1)
        if not _CODE_TOKEN_RE.match(token):
            refs.append(token)

    # Deduplicate while preserving order
    return list(dict.fromkeys(refs))


# ---------------------------------------------------------------------------
# Catalog loader
# ---------------------------------------------------------------------------


def load_catalog(catalog_path: Path) -> set[str]:
    """Return the set of label names defined in *catalog_path* (labels.yml).

    Parses without a YAML dependency by matching "- name: <value>" lines.
    """
    if not catalog_path.exists():
        print(f"ERROR: catalog file not found: {catalog_path}", file=sys.stderr)
        sys.exit(2)

    names: set[str] = set()
    name_re = re.compile(r"^\s*-?\s*name:\s*[\"']?(.+?)[\"']?\s*$")
    try:
        for line in catalog_path.read_text(encoding="utf-8").splitlines():
            m = name_re.match(line)
            if m:
                names.add(m.group(1).strip())
    except OSError as exc:
        print(f"ERROR: cannot read catalog: {exc}", file=sys.stderr)
        sys.exit(2)

    if not names:
        print(f"ERROR: no label names found in {catalog_path}", file=sys.stderr)
        sys.exit(2)

    return names


# ---------------------------------------------------------------------------
# Doc scanner
# ---------------------------------------------------------------------------


def scan_doc(doc_path: Path, catalog: set[str]) -> list[tuple[int, str]]:
    """Scan *doc_path* and return (line_number, label_name) mismatches."""
    if not doc_path.exists():
        print(f"WARNING: doc file not found, skipping: {doc_path}", file=sys.stderr)
        return []

    try:
        lines = doc_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"WARNING: cannot read {doc_path}: {exc}", file=sys.stderr)
        return []

    mismatches: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()

    for lineno, line in enumerate(lines, start=1):
        for ref in _extract_label_refs_from_line(line):
            ref = ref.strip()
            if ref and ref not in catalog:
                key = (lineno, ref)
                if key not in seen:
                    mismatches.append(key)
                    seen.add(key)

    return mismatches


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Detect drift between the label catalog (.github/labels.yml) "
            "and label names referenced in contributor docs."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--catalog",
        default=str(DEFAULT_CATALOG),
        help="Path to the label catalog YAML file (default: .github/labels.yml)",
    )
    parser.add_argument(
        "--docs",
        nargs="+",
        default=[str(p) for p in DEFAULT_DOCS],
        help="Markdown files to scan for label references (default: CONTRIBUTING.md, docs/good-first-issues.md)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print the full label catalog before scanning.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    catalog_path = Path(args.catalog)
    doc_paths = [Path(p) for p in args.docs]

    # -- Load catalog --------------------------------------------------------
    catalog = load_catalog(catalog_path)

    if args.verbose:
        print(f"Catalog ({len(catalog)} labels) loaded from: {catalog_path}")
        for name in sorted(catalog):
            print(f"  - {name}")
        print()

    # -- Scan docs -----------------------------------------------------------
    all_mismatches: dict[str, list[tuple[int, str]]] = {}
    for doc_path in doc_paths:
        mismatches = scan_doc(doc_path, catalog)
        if mismatches:
            all_mismatches[str(doc_path)] = mismatches

    # -- Report --------------------------------------------------------------
    if not all_mismatches:
        print("[OK] No label drift detected. All referenced labels exist in the catalog.")
        return 0

    print("[FAIL] Label drift detected -- the following label names are referenced")
    print("       in contributor docs but are MISSING from the catalog:\n")
    print(f"  Catalog: {catalog_path}\n")

    for doc_file, mismatches in all_mismatches.items():
        try:
            display = str(Path(doc_file).relative_to(REPO_ROOT))
        except ValueError:
            display = doc_file
        print(f"  File: {display}")
        for lineno, label in mismatches:
            print(f"    line {lineno:>4}: `{label}`")
        print()

    sep = "-" * 70
    print(sep)
    print("How to fix")
    print(sep)
    print("""
  Option A -- Add the missing label to the catalog:
    Edit .github/labels.yml and add an entry:
      - name: <missing-label>
        color: "<hex-colour>"
        description: <short description>
    Then sync live labels:
      python3 scripts/sync_github_labels.py --dry-run   # preview
      python3 scripts/sync_github_labels.py             # apply

  Option B -- Update the doc to use the current label name:
    Replace the stale label reference with the renamed/current equivalent.

  Option C -- The label was intentionally removed:
    Remove all references to it from contributor-facing docs.

  After fixing, re-run to confirm:
    python3 scripts/check_label_refs.py
""")

    return 1


if __name__ == "__main__":
    sys.exit(main())
