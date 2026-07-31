# Historical Reproduction Check

Date: 2026-07-17

## Target

The new condition `waggle_graph_guided_transcript_context` is intended to reproduce the historical `waggle_full` context path without mutating old result files.

## Execution Path

| Step | Historical Path | New Path | Status |
| --- | --- | --- | --- |
| Ingestion | `MemoryGraph.observe_conversation` via old runner | Same ingestion helper style through `build_case_graph` | Functionally equivalent |
| Context builder | `_context_from_waggle(... condition="waggle_full")` | Calls `_context_from_waggle(... condition="waggle_full")` directly | Exact function reuse |
| Condition name | `waggle_full` | `waggle_graph_guided_transcript_context` | Intentional rename in new rows |
| Context budget | Old run-specific budget | New `--reader-context-budget` truncates final context | Material difference if budget is lower than old output |
| Trace schema | Old JSONL row fields | New `context_items` wrapper around legacy context | Materially different trace representation |

## Evidence

- Adapter entry: `scripts/longmemeval_full/conditions.py:156-182`
- Legacy call: `condition="waggle_full"` at `scripts/longmemeval_full/conditions.py:160-164`
- Test coverage: `tests/test_longmemeval_full_conditions.py::test_graph_guided_condition_reproduces_old_context_path`

## Classification

**Functionally equivalent for context text, materially different for trace schema.**

The new condition is safe for side-by-side dry-run comparison, but it should not be claimed to reproduce every historical row field until a row-level migration/comparison utility checks:

- retrieved graph node IDs,
- selected source sessions,
- transcript record IDs,
- context text,
- context-token count,
- retrieval trace.

## Validation Gate

Historical reproducibility is **not fully passed**. It is adequate as a compatibility condition in the scaffold, but not enough to compare new results to old historical numbers without the documented trace-schema caveat.
