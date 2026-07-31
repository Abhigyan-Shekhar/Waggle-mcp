# Full-Capability LongMemEval Evaluation Report

This report was generated from a dry run. It validates ingestion, retrieval/context paths, budgets, and provenance serialization without paid reader or judge calls.

## Configuration

- Dataset: `longmemeval_s_existing`
- Dataset SHA: `328ba2dd782e869c9b078c724e6512fcf7dd08e4ead7d2243d753aa153b19595`
- Git commit: `5fc562bb6c666bed7593d459e6d12458e39559aa`
- Config SHA: `19394ced3ca5cfb2f897f3957e4eaf38818ca52614601a750311136dbbc1347b`
- Context budget: `3900`
- Conditions: `waggle_production_context`

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
