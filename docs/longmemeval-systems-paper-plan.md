# Waggle LongMemEval Systems-Paper Plan

This plan is the send-ready Lisa-facing version of the Waggle LongMemEval work.
It frames Waggle as a systems paper, not a benchmark paper.

## Research framing

The research question is whether Waggle's typed temporal graph and
context-assembly pipeline improve long-term memory QA over flat vector
retrieval while reducing injected context.

Do not cite a README-only benchmark number as evidence. Existing
retrieval/support-selection work may be described as prior internal work, but
any paper claim must be backed by a reproducible artifact: dataset hash, prompt
version, model ID, result JSONL, run manifest, and cost log.

Follow the Supermemory LongMemBench methodology where it is concrete and
reproducible:

- Ingest LongMemEval session-by-session, not round-by-round.
- For Waggle retrieval conditions, retrieve memory-like atomic summaries first
  and attach the original source chunks for answer generation.
- Preserve both `documentDate` and `eventDate` in the retrieval payload for
  knowledge-update and temporal-reasoning questions.
- Use an answering prompt that tells the reader model to scan memory summaries,
  then rely on chunks as the primary source of detail.
- Keep the official LongMemEval paper's question-specific judge prompts for
  answer evaluation with GPT-4o unless a judge change is explicitly justified.

User studies are out of scope for this phase. If "transparency" becomes a
human-facing metric, treat that as a separate study design that needs
ethics/data-regulation review with Lisa and local academic guidance.

## Required artifacts

Every paid or mock run must produce:

- A JSONL result file following
  [longmemeval-result-row.schema.json](./longmemeval-result-row.schema.json).
- A run manifest following
  [longmemeval-run-manifest.schema.json](./longmemeval-run-manifest.schema.json).
- A cost ledger whose total paid spend fits the single all-in cap below.
- Aggregated tables that keep retrieval metrics and judged QA metrics separate.

The run manifest must include `retrieval_config` entries for every
retrieval-assisted condition. `flat_vector`, `waggle_full`, and Waggle ablations
must share the same `embedding_model` and `chunking_policy`; preflight fails if
this parity is broken.

The manifest must also record:

- `ingestion_protocol=session-by-session`
- `answering_prompt_style=supermemory-longmembench-appendix-v1`
- `judge_protocol=longmemeval-paper-question-specific-prompts`

For retrieval conditions, `flat_vector` stays chunk-only except for the
temporal-reasoning parity path, where it expands top vector hits to the same
effective source-session budget used by `waggle_full`. It remains graph-free and
memory-free. `waggle_full` and Waggle ablations use `memory_then_chunk`
retrieval with `memory_plus_source_chunk` answer context.

## Diagnostic provenance notes

Do not describe the temporal-reasoning context change as a general retrieval
improvement. It is an inspection-derived, category-conditioned context-budget
patch: after held-out TR failures showed one-of-two support-session coverage,
TR runs use a larger effective context-session limit than the base
`retrieval_limit`. This budget increase must apply equally to `flat_vector` and
`waggle_full`: flat expands vector hits to the same number of source sessions,
while Waggle uses the same source-session budget plus its structured memory
sections. Result rows must log both the requested `retrieval_limit` and the
`effective_retrieval_limit`.

Report the current TR diagnostic honestly: increasing the effective TR context
limit fixes two of three inspected shortfalls, but `4dfccbf8` still misses one
labeled gold support session even at the larger budget. Treat this as open
long-tail risk, not as "TR solved." The inspected TR held-out rows are spent for
retrieval diagnostics and cannot be reused as blind evidence.

Use `scripts/validate_longmemeval_artifacts.py` before treating a run as valid.
The validator enforces the row shape, budget cap, held-out split protection, and
the rule that supplementary stress-test rows cannot be merged into the official
LongMemEval table.

## Run scaffold

Run the dry-run artifact pipeline first as a full plumbing smoke test. This
creates split, manifest, mock JSONL, validation, cost ledger, and summary
artifacts without paid model calls:

```bash
python3 scripts/run_longmemeval_artifact_pipeline.py \
  /path/to/longmemeval_s_cleaned.json \
  --output-dir runs/longmemeval/mock-001 \
  --mock-size 30 \
  --heldout-size 100 \
  --condition full_context \
  --condition flat_vector \
  --condition waggle_full
```

Create the ID-only mock/tune/heldout split plan and initial manifest before any
model calls:

```bash
python3 scripts/plan_longmemeval_run.py /path/to/longmemeval_s_cleaned.json \
  --output-dir runs/longmemeval/mock-001 \
  --mock-size 30 \
  --heldout-size 100 \
  --prompt-version longmemeval-systems-v1
```

Run the mock phase first in dry-run mode to validate artifact plumbing, then in
Gemini mode only when `GEMINI_API_KEY` is present:

```bash
python3 scripts/run_longmemeval_mock_phase.py /path/to/longmemeval_s_cleaned.json \
  --split-plan runs/longmemeval/mock-001/split-plan.json \
  --output runs/longmemeval/mock-001/results.jsonl \
  --condition full_context \
  --condition flat_vector \
  --condition waggle_full
```

For real Gemini token measurements, add `--mode gemini --reader-model
gemini-2.5-flash` and record the applicable token prices if paid usage should
be reflected in `cost_usd`.

The mock runner should already emit the Supermemory-style answer prompt shape:
question + question date, followed by retrieved memories/chunks and explicit
temporal reasoning instructions. Treat that prompt shape as part of the
reproducibility surface.

Before any paid run, preflight the manifest, dataset hash, split plan, provider
keys, heldout policy, and budget projection:

```bash
python3 scripts/preflight_longmemeval_run.py \
  runs/longmemeval/mock-001/run-manifest.json \
  --split-plan runs/longmemeval/mock-001/split-plan.json \
  --budget-projection runs/longmemeval/mock-001/budget-projection.json \
  --max-paid-cost 180
```

Validate every result JSONL against the run manifest:

```bash
python3 scripts/validate_longmemeval_artifacts.py \
  runs/longmemeval/mock-001/results.jsonl \
  --manifest runs/longmemeval/mock-001/run-manifest.json \
  --max-paid-cost 180
```

Export the cost ledger required for spend audit:

```bash
python3 scripts/export_longmemeval_cost_ledger.py \
  runs/longmemeval/mock-001/results.jsonl \
  --manifest runs/longmemeval/mock-001/run-manifest.json \
  --output-json runs/longmemeval/mock-001/cost-ledger.json \
  --output-md runs/longmemeval/mock-001/cost-ledger.md \
  --max-paid-cost 180
```

Generate separated retrieval, QA, and efficiency summaries. The summary keeps
official LongMemEval-S rows, non-official mock rows, and supplementary stress
rows in separate sections:

```bash
python3 scripts/summarize_longmemeval_results.py \
  runs/longmemeval/mock-001/results.jsonl \
  --output-json runs/longmemeval/mock-001/summary.json \
  --output-md runs/longmemeval/mock-001/summary.md \
  --max-paid-cost 180
```

Project optional rows from measured mock tokens before approving spend. For
example, Qwen/Qwen3.7-Plus full-context at 500 cases:

```bash
python3 scripts/project_longmemeval_budget.py \
  runs/longmemeval/mock-001/results.jsonl \
  --target full_context,Qwen/Qwen3.7-Plus,500,0.32,1.28 \
  --cap 180
```

## Conditions

Use the same chunking and embedding model for `flat_vector` and `waggle_full`.
The flat-vector baseline must isolate the graph/context-assembly contribution,
not introduce a different retrieval stack.

| Condition | Meaning |
|---|---|
| `full_context` | Entire LongMemEval history is placed in the reader prompt. |
| `flat_vector` | Top-k vector retrieval only, no graph, temporal, or conflict/update logic. |
| `waggle_full` | Hybrid retrieval, temporal scoring, graph expansion, conflict/update handling, and token-budgeted packing. |
| `ablation_semantic_only` | Waggle pipeline with lexical and temporal lanes removed. |
| `ablation_lexical_only` | Waggle pipeline with semantic and temporal lanes removed. |
| `ablation_temporal_only` | Waggle pipeline with semantic and lexical lanes removed. |
| `ablation_no_graph_expansion` | Waggle pipeline with direct hits only. |
| `ablation_no_conflict_update` | Waggle pipeline without update/contradiction resolution. |

## Experiment matrix and budget

Mock validation runs first on 20-30 stratified LongMemEval-S cases using Gemini
2.5 Flash or Flash-Lite. The mock phase must measure real input and output
tokens for full-context and retrieval-assisted prompts before any optional
full-context model row is approved.

All paid runs together must stay under **$180 total**, leaving Lisa's remaining
**$20 untouched** as retry and error buffer. This all-in cap includes the anchor,
Waggle/flat-vector runs, ablations, supplementary stress tests, judge calls,
and retries.

Paid priority order:

1. Gemini 2.5 Flash full-context anchor, 500 cases.
2. Llama 3.3 70B: `waggle_full` vs `flat_vector`, 500 cases each.
3. Qwen/Qwen3.7-Plus: `waggle_full` vs `flat_vector`, 500 cases each.
4. Llama 3.3 70B ablations: all five ablation conditions.
5. Supplementary stress tests: 90 cases, `waggle_full` vs `flat_vector`,
   reported separately from LongMemEval-S.
6. Claude Sonnet 5: 150 stratified cases, only if budget remains.

GPT-4o is treated as available for judging and optional reader comparison, but
all GPT-4o usage must fit the same $180 total cap.

Qwen/Qwen3.7-Plus full-context is not pre-approved. Run it only if the mock
phase measures enough token data to project the 500-case full-context cost and
the projected spend still fits under the same $180 all-in cap.

## Evaluation rules

Keep retrieval and QA separate:

- Retrieval: `Recall@5`, `Recall@10`, `Recall@15`, and exact support coverage.
- QA: judged end-to-end answer correctness with category breakdowns.
- Efficiency: mean injected context tokens, input tokens, output tokens,
  latency, and cost per condition.

Report LongMemEval-S categories separately:

- `SSU`
- `SSA`
- `SSP`
- `KU`
- `TR`
- `MS`

Create a stratified 100-question held-out split before tuning. Do not inspect,
aggregate, or tune against held-out rows until final evaluation. The validator
fails held-out rows unless `--allow-heldout` is passed deliberately.

Supplementary stress-test categories are reported separately:

- `adversarial_contradictions`
- `cross_session_chains`
- `agent_decision_memory`

Rows from the supplementary suite must set `official_table_eligible=false`.

## Pre-run checklist

- The dataset file exists and its SHA-256 hash is recorded.
- The prompt version is recorded.
- Model IDs are exact provider IDs.
- API keys are present only for the providers used in that run.
- Mock token counts exist before optional full-context rows.
- The projected total spend fits under $180.
- The held-out split remains unopened until final evaluation.
- No README-only benchmark number is used as evidence.
