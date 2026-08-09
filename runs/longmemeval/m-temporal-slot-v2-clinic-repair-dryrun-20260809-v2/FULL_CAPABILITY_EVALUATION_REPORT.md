# Full-Capability LongMemEval Evaluation Report

This report was generated from a dry run. It validates ingestion, retrieval/context paths, budgets, and provenance serialization without paid reader or judge calls.

## Configuration

- Dataset: `longmemeval_m_fresh`
- Dataset SHA: `7158160229df1698009f6b7cd4bcb0e60b1ffb89e182f330bfe8e5a6783683ae`
- Git commit: `2c15317f6fc25c970150d1c36e55a904afbe525a`
- Config SHA: `a21fe5faa128e9a486545ead56c5734692c9983b7728539bd3e04c734b6b966e`
- Context budget: `3900`
- Conditions: `waggle_temporal_slot_context`

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
