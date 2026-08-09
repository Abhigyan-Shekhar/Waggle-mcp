# Full-Capability LongMemEval Evaluation Report

This report was generated from a dry run. It validates ingestion, retrieval/context paths, budgets, and provenance serialization without paid reader or judge calls.

## Configuration

- Dataset: `longmemeval_m_fresh`
- Dataset SHA: `c28beaf1296a812cd86249d60e2ace1114cb2f7f18b5d1af4af8ef7826770078`
- Git commit: `5b3c6ab2f1ac31cd7d1eb152d56e3ea72d94929a`
- Config SHA: `45ee37db7bc87e73b8be84fca2ce362aec977dee76736b68d4d5897aa3256d20`
- Context budget: `3900`
- Conditions: `flat_transcript_vector`, `oracle_answer_turn_context`, `waggle_production_context`, `waggle_temporal_slot_context`

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
