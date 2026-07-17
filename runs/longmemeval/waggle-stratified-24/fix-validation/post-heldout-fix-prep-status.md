# Post-Heldout Fix Preparation Status

This work starts after the spent 71-case heldout baseline tagged `longmemeval-heldout-71-posthoc-baseline` at commit `b92f6dd6`. The 71-case slice remains immutable and was not re-scored after code changes.

## Step 0: Remaining Fresh Data

Useful estimate, excluding broad split-plan manifests and counting actual outputs/diagnostics/reviews as spent:

- Total LongMemEval-S cases: 500
- Spent or inspected: 484
- Remaining never-inspected estimate: 16
- Remaining categories: KU 4, SSU 5, MS 6, TR 1
- Remaining IDs are all abstention-style IDs in the current estimate.

Conclusion: there is no clean fresh 71-case LongMemEval-S validation slice left in this local run state. Future validation must either use a smaller n=16 abstention-heavy slice, draw from a different source/split, or clearly label any reused cases as development/diagnostic only.

## Step 1: Recency/Conflict Fix

Patched `scripts/run_longmemeval_waggle_phase.py` prompt assembly:

- Added a global conflict and recency rule.
- Added direct-user-statement priority over assistant-inferred, tentative, or hedged values.
- Added caveat for questions explicitly asking for older/previous/first values.
- Added temporal instruction to use the source event date rather than later question/recommendation dates.

Static validation artifact: `static-context-validation-after-recency-precise-source.json`.

## Step 2: Precise Source Context Fix

Patched context assembly for exact/list/example/day-of-week questions:

- Detects ordinal/list/example/exact/day-of-week question patterns.
- Suppresses low-value structured memory sections in those cases.
- Renders fuller raw source messages as `Precise Source Transcript Chunks`.
- Preserves source transcript wording in the reader prompt.

Static validation showed:

- `8752c811`: `Sound effects`, `ambient`, and `diegetic` now appear in Waggle context.
- `f523d9fe`: `Doc Martin` and `last season` now appear in Waggle context.
- `dfde3500`: after day-of-week detection, `Wednesday` now appears in Waggle context.

No LLM re-score was run on the spent 71 cases.

## Step 3: MS Coverage Audit

Artifact: `ms-miss-session-coverage-audit.json`.

MS misses split as:

- Full support but answer failure: `2311e44b`, `73d42213`, `80ec1f4f`, `bf659f65`.
- Shared coverage shortfall for both flat and Waggle: `ba358f49`, `bc149d6b`, `gpt4_31ff4165`, `gpt4_a56e767c`.

Conclusion: no new Waggle-only MS locality bug was found in the 71-case review; the unsolved MS retrieval gaps are shared with flat under the current setup.

## Step 4: Second Judge

Ran second-model adjudication on the 24 answers from the 12 Llama-judge disagreement cases using Groq `qwen/qwen3-32b`.

Artifacts:

- `second-judge-disagreements-qwen3-32b.jsonl`
- `second-judge-disagreements-qwen3-32b-rerun-unparsed.jsonl`
- `second-judge-disagreements-qwen3-32b-normalized.jsonl`
- `second-judge-disagreements-qwen3-32b-final.jsonl`

Qwen-adjusted totals, replacing only the 12 disagreement cases and leaving the rest as original Llama-judge labels:

- flat_vector: 55/71
- waggle_full: 53/71

This confirms the raw result remains mixed/negative for Waggle, but the margin narrows after independent disagreement adjudication.
