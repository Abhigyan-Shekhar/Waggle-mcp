# `.abhi` Test Fixtures

This directory contains canonical `.abhi` fixture files used by the
`tests/test_abhi_diff_merge.py` test suite.

## Regenerating Fixtures

If the `.abhi` format changes, regenerate all fixtures by running:

```bash
PYTHONPATH=src python3 scripts/generate_abhi_fixtures.py
```

The script (`scripts/generate_abhi_fixtures.py`) is **not** run at test time.
It is only used to regenerate the committed fixture files when the format changes.

---

## Fixture Descriptions

### `empty.abhi`

- **Nodes:** 0
- **Edges:** 0
- **Purpose:** Baseline for diff/merge tests that need an empty document.
  Diffing any document against `empty.abhi` should show all nodes/edges as added.

---

### `single-node.abhi`

- **Nodes:** 1
- **Edges:** 0
- **Purpose:** Minimal non-empty document. Useful for testing that a single
  node is correctly detected as added when diffed against `empty.abhi`.

---

### `linear-history.abhi`

- **Nodes:** 10
- **Edges:** 9 (`relates_to` edges forming a linear chain: n0→n1→…→n9)
- **Purpose:** Tests diff/merge on a simple sequential graph with no branching.
  Diffing this file against itself should produce zero changes.

---

### `branched.abhi`

- **Nodes:** 20
- **Edges:** 25 (backbone + three branches + cross-links)
- **Purpose:** Tests diff/merge on a more complex graph with branching and
  cross-links. Exercises the full edge-traversal logic.

---

### `with-contradictions.abhi`

- **Nodes:** 4
- **Edges:** 3 (two `contradicts` edges, one `relates_to` edge)
- **Purpose:** Tests that `contradicts` relationship edges are correctly
  preserved through diff/merge operations.

---

### `with-dangling-edges.abhi` ⚠️ Intentionally Invalid

- **Nodes:** 2 (n1, n2)
- **Edges:** 2 (e1: n1→n2 valid; e2: n1→*missing_node* **dangling**)
- **Purpose:** Tests boundary enforcement. This file is **intentionally
  invalid** — it contains an edge (`e2`) whose `target_id` does not correspond
  to any node in the document.

  Expected behaviour:
  - `validate_abhi_document()` → `valid=True` but `dangling_edge_count > 0`
    (dangling edges are reported as warnings, not errors, by the validator)
  - `abhi_to_snapshot()` without flags → raises `DanglingEdgeError`
  - `abhi_to_snapshot(allow_dangling=True)` → succeeds, drops the dangling edge
  - `abhi_to_snapshot(force=True)` → succeeds, drops the dangling edge

---

### Secret-scan transcript cases

These JSON fixtures are used by the server export tests to exercise the
transcript secret scan without committing real credentials:

- `secret-scan-refusal.json` contains an obvious fake token shape that should
  trigger a refusal unless `--force` is set.
- `secret-scan-safe.json` contains nearby wording such as "password policy"
  and "API key rotation plan" that should remain exportable.
