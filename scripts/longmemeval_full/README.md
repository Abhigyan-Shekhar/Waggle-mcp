# LongMemEval Full-Capability Runner

This module adds a new evaluation path without modifying the historical LongMemEval runner or result files.

## Implementation Plan

1. Preserve the old conditions under accurate new names:
   - `flat_vector` -> `flat_transcript_vector`
   - `waggle_full` -> `waggle_graph_guided_transcript_context`
2. Add deterministic production-capability conditions:
   - `waggle_production_context`
   - `waggle_temporal_slot_context`: relation-aware state projection, independent evidence-slot retrieval, conservative local calculations, and compact compiled context.
   - `waggle_production_context_no_prime`
   - `waggle_production_context_with_prime`
   - `waggle_context_without_temporal_resolution`
   - `waggle_context_with_temporal_resolution`
3. Add capability ablations:
   - `waggle_graph_nodes_only`
   - `waggle_hybrid_no_edges`
   - `waggle_hybrid_with_edges`
   - `oracle_support_context`
4. Add bounded dry-run agentic mode:
   - `waggle_agentic_mcp`
5. Enforce equal final reader-context budgets across conditions.
6. Serialize provenance, retrieval traces, tool traces, and report artifacts.
7. Keep paid reader/judge execution disabled until a frozen manifest and cost estimate are reviewed.

## Production Code Path Mapping

| Evaluation operation | Production function called | Adapter used? | Behavioural differences |
| -------------------- | -------------------------- | ------------: | ----------------------- |
| Ingestion | `MemoryGraph.observe_conversation` | Thin case loader | Per-case temporary SQLite graph |
| Flat transcript vector | `MemoryGraph.search_transcript_records` | Thin formatter | Does not call graph retrieval |
| Historical graph-guided context | `scripts.run_longmemeval_waggle_phase._context_from_waggle` | Compatibility adapter | Old `waggle_full` output is renamed only in new rows |
| Production hybrid query | `MemoryGraph.query(... retrieval_mode="hybrid")` | Thin provenance wrapper | Mirrors MCP `query_graph` default retrieval mode |
| Production context | `RecursiveContextController.build_context` | Thin budget/provenance wrapper | No post-build large session transcript append |
| Temporal-slot context | `MemoryGraph.temporal_slot_retriever().retrieve(...)` | Thin LongMemEval formatter | Uses source timestamps, independent slot capacity, version-aware state, and a compact evidence compiler |
| Related-node inspection | `MemoryGraph.get_related` | Bounded agentic adapter | Node-scoped production API |
| Prime context | `MemoryGraph.prime_context` | Optional ablation adapter | Budgeted separately and deduped by final context budget |

## Dry Run

```bash
.runtime-build-venv/bin/python -m scripts.longmemeval_full.run \
  --dataset targeted_stress_v2 \
  --conditions all \
  --dry-run \
  --limit 1 \
  --reader-context-budget 512 \
  --output-dir /tmp/waggle-full-all-dryrun
```

Dry run validates ingestion, retrieval/context assembly, budget enforcement, provenance serialization, and report generation. It does not call paid reader or judge APIs.

For scientific API-free retrieval probes, add `--embedding-model real` so ingestion
and retrieval use Waggle's configured local embedding model. The deterministic
embedding mode is intended only for fast harness tests.

## Real Evaluation Gate

Before paid evaluation, freeze and review:

- `frozen_case_manifest.json`
- `config.json`
- selected conditions
- context budgets
- reader and judge models
- estimated calls and cost
- validation-test results

The CLI requires both `--allow-paid` and `GROQ_API_KEY` for non-dry-run execution.

## Blind Slice Workflow

Draw a balanced slice while conservatively excluding every case found in local
result files, frozen manifests, and derived LongMemEval-S datasets:

```bash
.runtime-build-venv/bin/python -m scripts.longmemeval_full.draw_fresh_slice \
  --source benchmarks/longmemeval/longmemeval_s_cleaned.json \
  --output benchmarks/longmemeval/longmemeval_s_temporal_slot_fresh24_20260802.json \
  --manifest benchmarks/longmemeval/longmemeval_s_temporal_slot_fresh24_20260802_manifest.json \
  --size 24 \
  --seed 20260802
```

The generated manifest records the source hash, exclusion-source hashes,
category counts, and a mandatory empty spent-overlap list. Do not inspect or
tune against selected case contents before the one-shot paid run.

After an API-free retrieval run, score session coverage without exposing case
content or using gold labels during retrieval:

```bash
.runtime-build-venv/bin/python -m scripts.longmemeval_full.audit_retrieval_coverage \
  --dataset benchmarks/longmemeval/longmemeval_s_temporal_slot_fresh24_20260802.json \
  --results runs/longmemeval/<run>/results.jsonl \
  --graph-cache-dir runs/longmemeval/<graph-cache> \
  --output runs/longmemeval/<run>/retrieval_coverage_aggregate.json
```

This audit uses gold session IDs only after retrieval to compute aggregate
coverage. It does not alter retrieval or context assembly. Omit
`--include-case-details` while preserving a blind slice.

## External Memory System QA Comparison

### First-Class Mem0 Condition

`mem0_context` compares against Mem0 OSS in the same end-to-end QA harness:

- ingestion uses `mem0.Memory.add()` per LongMemEval turn,
- retrieval uses `mem0.Memory.search()` with the question and `--retrieval-limit`,
- the reader and judge remain this runner's controlled Groq calls,
- contexts are cached as JSONL before QA so runs are resumable and auditable.

Install the optional dependency before running this condition:

```bash
.runtime-build-venv/bin/python -m pip install -e '.[mem0]'
```

By default, the adapter uses Mem0 OSS with `infer=False`, local Qdrant, and HuggingFace `all-MiniLM-L6-v2` embeddings at 384 dimensions. That keeps the embedding choice aligned with Waggle/flat as closely as Mem0's OSS API allows. The minimal optional install validates semantic Mem0 search; Mem0 may warn that optional BM25/NLP extras are unavailable unless `mem0ai[extras]` / spaCy resources are installed. To run Mem0's LLM extraction path instead, pass `--mem0-infer`; that changes the comparison because Mem0 then performs its own memory extraction.

Example dry run:

```bash
.runtime-build-venv/bin/python -m scripts.longmemeval_full.run \
  --dataset longmemeval_s_existing \
  --dataset-path benchmarks/longmemeval/some_frozen_slice.json \
  --conditions flat_transcript_vector,waggle_production_context,mem0_context \
  --reader-context-budget 3900 \
  --retrieval-limit 10 \
  --dry-run \
  --output-dir runs/longmemeval/mem0-comparison-dryrun \
  --graph-cache-dir runs/longmemeval/.graph-cache-mem0-comparison \
  --keep-graph-cache
```

Example paid smoke test:

```bash
GROQ_API_KEY=... .runtime-build-venv/bin/python -m scripts.longmemeval_full.run \
  --dataset longmemeval_s_existing \
  --dataset-path benchmarks/longmemeval/some_frozen_slice.json \
  --conditions flat_transcript_vector,waggle_production_context,mem0_context \
  --reader-context-budget 3900 \
  --retrieval-limit 10 \
  --limit 3 \
  --allow-paid \
  --reader-model llama-3.3-70b-versatile \
  --primary-judge-model llama-3.3-70b-versatile \
  --output-dir runs/longmemeval/mem0-comparison-paid-smoke \
  --graph-cache-dir runs/longmemeval/.graph-cache-mem0-comparison \
  --keep-graph-cache
```

The generated `mem0_contexts.jsonl` cache is part of the run artifact. Do not merge smoke rows or diagnosis rows into the official comparison table.

### Generic External JSONL

Use `external_jsonl:<system>` conditions to compare Waggle against other memory systems end-to-end. External systems only provide retrieved/assembled memory context; this runner still controls:

- dataset slice,
- reader prompt,
- reader model,
- judge model,
- final context budget,
- result schema,
- provenance artifacts.

This keeps the metric as end-to-end QA, not retrieval-only recall.

External context JSONL schema:

```jsonl
{"case_id":"abc123","system":"mem0","context":"Retrieved context text for the reader."}
{"case_id":"abc123","system":"graphiti","context_items":[{"item_id":"edge-1","item_type":"graph_fact","text":"Current value: two free nights."}]}
```

Required fields:

- `case_id`
- `system`
- either `context` or `context_items`

Optional fields:

- `retrieval_mode`
- `retrieved_node_ids`
- `retrieved_transcript_ids`
- `retrieved_edge_ids`
- `source_evidence_ids`
- `adapter_notes`
- `metadata`

Example dry run:

```bash
.runtime-build-venv/bin/python -m scripts.longmemeval_full.run \
  --dataset longmemeval_s_existing \
  --dataset-path benchmarks/longmemeval/some_frozen_slice.json \
  --conditions waggle_production_context,external_jsonl:mem0,external_jsonl:graphiti \
  --external-context-path runs/longmemeval/external_contexts.jsonl \
  --dry-run \
  --output-dir runs/longmemeval/external-comparison-dryrun
```

Example paid QA run:

```bash
GROQ_API_KEY=... .runtime-build-venv/bin/python -m scripts.longmemeval_full.run \
  --dataset longmemeval_s_existing \
  --dataset-path benchmarks/longmemeval/some_frozen_slice.json \
  --conditions waggle_production_context,external_jsonl:mem0,external_jsonl:graphiti \
  --external-context-path runs/longmemeval/external_contexts.jsonl \
  --reader-context-budget 3900 \
  --retrieval-limit 10 \
  --allow-paid \
  --reader-model llama-3.3-70b-versatile \
  --primary-judge-model llama-3.3-70b-versatile \
  --output-dir runs/longmemeval/external-comparison-paid
```
