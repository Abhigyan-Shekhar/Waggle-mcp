# Full-Capability Scaffold Validation

Date: 2026-07-17

## Verdict

The scaffold is safer and more auditable after this validation pass, but it is **not ready for a paid pilot across all conditions**.

Ready for a small paid deterministic pilot after cost/model review:

- `flat_transcript_vector`
- `waggle_graph_guided_transcript_context` with historical-reproduction caveat
- `waggle_production_context`
- `waggle_graph_nodes_only`
- `oracle_support_context`

Implemented but insufficiently validated:

- `waggle_production_context_no_prime`
- `waggle_production_context_with_prime`
- `waggle_context_with_temporal_resolution`
- `waggle_context_without_temporal_resolution`

Partial placeholder / not ready for paid pilot:

- `waggle_hybrid_no_edges`
- `waggle_hybrid_with_edges`
- `waggle_agentic_mcp`

## Condition Audit Table

| Condition | Entry function | Production Waggle functions called | Test/evaluation adapter logic | Stubbed behavior? | Scientifically valid? |
| --------- | -------------- | ---------------------------------- | ----------------------------- | ----------------: | --------------------: |
| `flat_transcript_vector` | `flat_transcript_vector` (`conditions.py:131`) | `MemoryGraph.search_transcript_records` (`src/waggle/graph/transcript.py:916`) | Formats replay hits and enforces budget | No reader/judge in dry run | Yes for transcript-only baseline |
| `waggle_graph_guided_transcript_context` | `graph_guided_transcript_context` (`conditions.py:156`) | Legacy `_context_from_waggle(... waggle_full)` (`scripts/run_longmemeval_waggle_phase.py:1002`) | Wraps historical context as one item and applies new budget | No | Valid as compatibility condition; trace differs |
| `waggle_production_context` | `production_context` (`conditions.py:185`) | `debug_retrieval`, `graph.query(... hybrid)`, `RecursiveContextController.build_context` | Adds trace and budget wrapper; does not append sessions after build | No reader/judge in dry run | Conditionally valid for production context pilot |
| `waggle_production_context_no_prime` | `production_context` | Same as production without `prime_context` | Alias of production path with explicit name | No | Valid only as naming/control row |
| `waggle_production_context_with_prime` | `production_context(use_prime=True)` | `prime_context`, `debug_retrieval`, `query`, `build_context` | Adds bounded prime item before final budget pack | No | Needs fixture evidence that prime helps rather than adds noise |
| `waggle_graph_nodes_only` | `graph_nodes_only` (`conditions.py:290`) | `MemoryGraph.query(... retrieval_mode="graph")` | Formats extracted nodes only; no transcript recovery | No | Valid graph-node ablation |
| `waggle_hybrid_no_edges` | `hybrid_context(include_edges=False)` (`conditions.py:316`) | `debug_retrieval`, `MemoryGraph.query(... hybrid)` | Suppresses returned edge context | No | Not clean: internal HybridRetriever graph expansion still active |
| `waggle_hybrid_with_edges` | `hybrid_context(include_edges=True)` | Same as no-edge plus returned edges | Includes returned edges in final context | No | Not clean until no-edge truly disables graph expansion |
| `waggle_context_without_temporal_resolution` | `production_context(temporal_resolution=False)` | `build_context(ablation=conflict_resolve=False)` | Disables one conflict-resolution flag | No | Insufficiently isolated temporal ablation |
| `waggle_context_with_temporal_resolution` | `production_context(temporal_resolution=True)` | full `build_context` conflict/update handling | Default production path | No | Needs deterministic temporal fixture proof |
| `waggle_agentic_mcp` | `agentic_mcp` (`conditions.py:366`) | `prime_context`, `query(... hybrid)`, `get_related` | Deterministic fixed tool sequence | Yes: no model tool selection | Partial placeholder |
| `oracle_support_context` | `oracle_support_context` (`conditions.py:428`) | `list_transcript_records` over gold sessions | Explicit oracle adapter | Uses gold support IDs by design | Valid only as upper bound |

## Production Function Details

| Production function | File path | Relevant arguments | Returned representation | Adapter differences |
| --- | --- | --- | --- | --- |
| `MemoryGraph.observe_conversation` | `src/waggle/graph/transcript.py:263` | `user_message`, `assistant_response`, `project`, `agent_id`, `session_id` | observation result with turn/node/edge counts | Per-case temporary SQLite graph |
| `MemoryGraph.search_transcript_records` | `src/waggle/graph/transcript.py:916` | `query`, `project`, `limit` | list of `ReplayHit` | Adapter formats transcript hits as context items |
| `MemoryGraph.query` | `src/waggle/graph/traversal.py:211` | `query`, `project`, `agent_id`, `max_nodes`, `retrieval_mode` | `SubgraphResult` with nodes/edges/replay/hybrid hits | Used directly; condition controls mode |
| `MemoryGraph.debug_retrieval` | `src/waggle/graph/traversal.py:564` | `query`, `project`, `agent_id`, `retrieval_mode="hybrid"` | layer summaries, hybrid hits, fused top20 | Added for audit trace, not final selection |
| `RecursiveContextController.build_context` | `src/waggle/recursive_context.py:178` | `query`, `project`, `agent_id`, `token_budget`, `depth`, `ablation` | `RecursiveContextResult` context pack | Final context uses returned pack directly |
| `MemoryGraph.prime_context` | `src/waggle/graph/__init__.py:3850` | `project`, `agent_id`, `max_nodes` | subgraph-like result | Optional bounded pre-context item |
| `MemoryGraph.get_related` | `src/waggle/graph/traversal.py:1227` | `node_id`, `max_depth` | related nodes/edges | Agentic simulation inherits scope from selected node |
| `MemoryGraph.list_transcript_records` | `src/waggle/graph/transcript.py:853` | `project`, `session_id`, `limit` | transcript records | Used only by oracle condition |

## Validation Matrix

| Condition | Intended capability | Real production path verified? | Behavioral difference demonstrated? | Budget-safe? | Oracle-safe? | Ready? |
| --------- | ------------------- | -----------------------------: | ----------------------------------: | -----------: | -----------: | -----: |
| `flat_transcript_vector` | transcript replay baseline | yes | yes | yes | yes | ready for paid pilot |
| `waggle_graph_guided_transcript_context` | historical compatibility | yes | yes vs production path | yes | yes | implemented but trace caveat |
| `waggle_production_context` | hybrid + `build_context` | yes | yes | yes | yes | ready for small pilot |
| `waggle_production_context_no_prime` | production context without priming | yes | control row only | yes | yes | implemented but insufficiently validated |
| `waggle_production_context_with_prime` | bounded `prime_context` contribution | yes | not yet proven useful | yes | yes | implemented but insufficiently validated |
| `waggle_graph_nodes_only` | extracted node-only ablation | yes | yes | yes | yes | ready for small pilot |
| `waggle_hybrid_no_edges` | hybrid without graph edges | partially | no, internal graph expansion still active | yes | yes | scientifically invalid as edge ablation |
| `waggle_hybrid_with_edges` | hybrid plus edge context | partially | context differs, retrieval not cleanly isolated | yes | yes | scientifically invalid as edge ablation |
| `waggle_context_without_temporal_resolution` | disable temporal/update/conflict logic | partially | not fully isolated | yes | yes | implemented but insufficiently validated |
| `waggle_context_with_temporal_resolution` | full temporal/update/conflict logic | partially | not yet fixture-proven | yes | yes | implemented but insufficiently validated |
| `waggle_agentic_mcp` | model-driven MCP tool use | no | deterministic sequence only | yes | yes | partial placeholder |
| `oracle_support_context` | gold support upper bound | yes | yes | yes | oracle by design | ready as oracle only |

## Tests Added/Run

No-cost commands:

```bash
.runtime-build-venv/bin/pytest -q tests/test_longmemeval_full_conditions.py tests/test_longmemeval_full_cli.py
```

Current result:

```text
13 passed
```

Dry-run validation:

```bash
.runtime-build-venv/bin/python -m scripts.longmemeval_full.run \
  --dry-run \
  --limit 2 \
  --conditions all \
  --reader-context-budget 512 \
  --retrieval-limit 5 \
  --max-tool-calls 6 \
  --output-dir /tmp/waggle-full-validate-dryrun

.runtime-build-venv/bin/python -m scripts.longmemeval_full.validate_artifacts \
  /tmp/waggle-full-validate-dryrun
```

Current result: artifact validation passes.

## Gate Status

| Gate | Status | Notes |
| --- | --- | --- |
| Gate A: Production path | partial | Hybrid and `build_context` verified; edge/temporal ablations not clean. |
| Gate B: Historical reproducibility | partial | Context function reused; trace schema differs. |
| Gate C: Behavioral fixtures | partial | Fixtures exist and hybrid/build-context tests pass; temporal/edge behavior not yet sufficient. |
| Gate D: Fairness | partial | Budgets enforced; dry-run no-oracle fixed; category still appears in reporting but not prompt/context. |
| Gate E: Artifacts | pass for dry run | `validate_artifacts.py` passes on dry run. |
| Gate F: Scientific naming | pass | No condition is named `waggle_full`; agentic mode is labelled simulation in traces/docs. |

## Final Recommendation

Do **not** enable a broad paid pilot yet.

A small paid pilot is defensible only for:

```text
flat_transcript_vector
waggle_graph_guided_transcript_context
waggle_production_context
waggle_graph_nodes_only
oracle_support_context
```

Exclude for now:

```text
waggle_hybrid_no_edges
waggle_hybrid_with_edges
waggle_context_without_temporal_resolution
waggle_context_with_temporal_resolution
waggle_agentic_mcp
```

Those excluded conditions need cleaner production switches or stronger deterministic proof before their numbers can support scientific claims.
