# Full-Capability LongMemEval Evaluation Report

This report was generated from a dry run. It validates ingestion, retrieval/context paths, budgets, and provenance serialization without paid reader or judge calls.

## Configuration

- Dataset: `longmemeval_s_existing`
- Dataset SHA: `a5dcb84851b2e75ee8cb73532df77c4fd2618d996ded773973a11962b5cfb828`
- Git commit: `17aab755e80a0a4fa349fc8e89f19582b1e0eff4`
- Config SHA: `784b7162911df636103ab7dafca9e97579064e69ed36be9514599b58a72b4ed6`
- Context budget: `3900`
- Conditions: `external_jsonl:mempalace`, `waggle_production_context`

## Production Code Path Mapping

| Evaluation operation | Production function called | Adapter used? | Behavioural differences |
| -------------------- | -------------------------- | ------------: | ----------------------- |
| Ingestion | `MemoryGraph.observe_conversation` | Thin case loader | Per-case temporary SQLite graph |
| Flat transcript vector | `MemoryGraph.search_transcript_records` | Thin formatter | No graph nodes or edges included |
| Existing graph-guided context | legacy `_context_from_waggle(... condition='waggle_full')` | Historical adapter | Renamed only in new outputs |
| Production hybrid query | `MemoryGraph.query(... retrieval_mode='hybrid')` | Thin provenance wrapper | Mirrors MCP `query_graph` default retrieval mode |
| Full production context | `RecursiveContextController.build_context` | Thin budget/provenance wrapper | No post-build large session append |
| Agentic MCP | `prime_context`, `query`, `get_related` | Deterministic bounded tool runner | Simulates tool sequence without LLM tool selection in dry run |

## Current Limitation

Paid reader/judge execution is intentionally disabled in this implementation pass. Before paid evaluation, freeze the manifest and confirm model/cost configuration.

