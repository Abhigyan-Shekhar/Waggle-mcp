# Full-Capability LongMemEval Evaluation Report

This report was generated from a dry run. It validates ingestion, retrieval/context paths, budgets, and provenance serialization without paid reader or judge calls.

## Configuration

- Dataset: `targeted_stress_v2`
- Dataset SHA: `d764f2055db11ad3834ca7c477130bdc0dcf694987ff531496189da5aa0a46e6`
- Git commit: `324bddf04620ccbbc4078b9106a39ef7051ba5bf`
- Config SHA: `82f4f8a0c808453a0196246201a80cf72f1395930d391a755d9baebb67c6d94d`
- Context budget: `512`
- Conditions: `external_jsonl:graphiti_smoke`, `external_jsonl:mem0_smoke`, `waggle_production_context`

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
