# How Waggle Saves Tokens

LLMs have finite context windows. As a conversation grows longer, the naive
approach — stuffing the full history into every prompt — becomes expensive,
slow, and eventually impossible. Waggle solves this by replacing raw history
injection with a persistent knowledge graph and targeted retrieval.

---

## The Problem with Conversation History

Without a memory layer, every prompt looks like this:

```text
[System prompt]
[Turn 1 — user + assistant]
[Turn 2 — user + assistant]
  …
[Turn N — user + assistant]
[Current user message]
```

This pattern has two failure modes:

| Failure mode | What happens |
|---|---|
| **Token cost explosion** | History length grows linearly. A 10-turn session at ~300 tokens per turn costs ~3 000 context tokens per call. At 50 turns that becomes ~15 000 — even when 90% of the content is irrelevant to the current question. |
| **Truncation** | Once the window fills, older turns are silently dropped. The architecture decision from turn 3 disappears by turn 30, and there is no recovery path. |

Both problems have real consequences: longer prompts mean slower time-to-first-token
and higher per-call cost on every major inference provider.

---

## How Waggle Works Instead

Waggle separates *storage* from *injection*. Instead of holding the full
conversation in memory, it maintains a structured knowledge graph and retrieves
only what is relevant at each turn.

**Step 1 — Observe.** After each turn,
[`observe_conversation`](../src/waggle/graph.py) runs an extraction pass and
writes structured nodes — decisions, preferences, entities, facts — into a
SQLite-backed knowledge graph. The raw transcript is also stored verbatim for
evidence retrieval.

**Step 2 — Retrieve.** On the next turn,
[`build_context`](../src/waggle/recursive_context.py) decomposes the current
query into targeted subqueries and retrieves only the nodes whose cosine
similarity to the query exceeds the retrieval threshold.

**Step 3 — Budget.** [`RecursiveContextController._compress_to_budget`](../src/waggle/recursive_context.py#L684)
packs the retrieved nodes into a compact, prioritised string bounded by a
configurable token budget (default: **1 200 tokens**, set via
`WAGGLE_RECURSIVE_CONTEXT_DEFAULT_BUDGET`).

The prompt injected into each LLM call becomes:

```text
[System prompt]
[Waggle context pack — ≤ 1 200 tokens of targeted memory]
[Current user message]
```

Context size is now **O(1)** with respect to conversation length, not O(N).

---

## Worked Example — Architecture Decision Across Ten Turns

Consider a 10-turn engineering conversation where the database choice surfaces
three separate times:

> **Turn 2** — "We're going with PostgreSQL because we need analytics joins."  
> **Turn 6** — "Reminder: the DB is PostgreSQL, not SQLite."  
> **Turn 9** — "Just confirming — production is still PostgreSQL, right?"

### Without Waggle

Every call receives the full transcript. By turn 10, each prompt carries all
ten turns (~250 tokens each on average), and the same fact appears three times
with no added value.

```text
10 turns × ~250 tokens = 2 500 context tokens per call
Cumulative across 10 calls = ~13 750 context tokens
```

### With Waggle

After turn 2, `observe_conversation` writes a single decision node:

```text
Label:   "Database choice"
Content: "PostgreSQL chosen for production; required for analytics joins."
Type:    decision
```

When the same fact resurfaces in turns 6 and 9, the deduplication layer
([`_find_duplicate_node`](../src/waggle/graph.py)) recognises the semantic
equivalence via cosine similarity and merges the new observation into the
existing node rather than creating a duplicate.

At turn 10, `build_context` retrieves one node and returns:

```text
### Waggle Recursive Context Pack
Task: confirming production database

Current relevant decisions:
- [decision] Database choice: PostgreSQL chosen for production; required for analytics joins.
```

**Token cost: ~30 tokens.** The same information, one-hundredth of the weight.

---

## Benchmark Results

Token counts are computed with the `ceil(len(text) / 4)` heuristic used
throughout the codebase, with tiktoken `cl100k_base` applied automatically
when available. In `recursive_context.py`, real-time budget tracking uses
`len(text) // 4`, which can differ by at most one token per string. All runs use
`WAGGLE_MODEL=deterministic` — no API key required.

| Scenario | Turns | Recurring facts | Baseline tokens | Waggle tokens | Reduction |
|---|---|---|---|---|---|
| Architecture decisions (database, auth, cache) | 10 | 3 | ~2 500 | ~90 | **96%** |
| Debugging session (same stack trace re-raised) | 8 | 1 | ~1 600 | ~40 | **97%** |
| Onboarding Q&A (team and tooling questions repeat) | 12 | 5 | ~3 000 | ~150 | **95%** |

**Baseline** — full conversation history injected at the final turn.  
**Waggle** — context pack returned by `build_context` at that same turn, capped
at the 1 200-token budget.

---

## The Three Sources of Savings

### 1. Deduplication

[`_find_duplicate_node`](../src/waggle/graph.py) computes cosine similarity
between each incoming node embedding and all existing nodes of the same type.
When the similarity exceeds `dedup_similarity_threshold` (default: **0.92**),
the observation is merged into the existing node. No matter how many times a
fact is mentioned in conversation, it occupies exactly **one slot** in the
graph.

### 2. Relevance Filtering

[`HybridRetriever.retrieve`](../src/waggle/retrieval/hybrid.py) scores
candidates across four independent signals and fuses them with Reciprocal Rank
Fusion (RRF):

| Signal | What it measures |
|---|---|
| `vector_transcript` | Semantic similarity to raw transcript turns |
| `vector_node` | Semantic similarity to stored graph node embeddings |
| `bm25` | Lexical keyword overlap ([`SimpleBM25`](../src/waggle/retrieval/hybrid.py#L140)) |
| `graph_expansion` | Connectivity via typed edges (`updates`, `depends_on`, `part_of`, …) |

Scores are further weighted by an exponential recency decay
(`recency_half_life_days`, default: **30 days**). Only the top-K candidates
pass to `build_context`; unrelated nodes never reach the prompt.

### 3. Token Budgeting

[`_compress_to_budget`](../src/waggle/recursive_context.py#L684) fills the
context pack in priority order — decisions, preferences, facts, conflicts —
and stops the moment the token estimate crosses the budget ceiling:

```python
# src/waggle/recursive_context.py
DEFAULT_TOKEN_BUDGET: int = _env_int("WAGGLE_RECURSIVE_CONTEXT_DEFAULT_BUDGET", 1200)
```

The budget is a target ceiling, not a hard limit. The implementation computes
`max_tokens = int(token_budget * 1.15)`, permitting up to ~15% overage to
avoid cutting off the last entry mid-section. In practice the context pack
remains tightly bounded and does not grow with conversation length.

---

## Token Counting — Implementation Notes

Two `_estimate_tokens` implementations exist in the codebase. They use the
same underlying formula and differ only in rounding:

| Module | Formula | Purpose |
|---|---|---|
| [`context_bundle.py`](../src/waggle/context_bundle.py#L24) | `ceil(len(text) / 4)` | Bundle export estimates |
| [`recursive_context.py`](../src/waggle/recursive_context.py#L820) | `len(text) // 4` | Real-time budget tracking |
| [`token_efficiency_benchmark.py`](../src/waggle/token_efficiency_benchmark.py#L224) | tiktoken `cl100k_base` when available, else `ceil(len/4)` | Benchmark measurements |

The difference between `ceil` and `floor` is at most one token per string —
negligible at the budget scale. Install `tiktoken` for model-accurate counts;
the benchmark harness enables it automatically.

---

## Running the Benchmark

```bash
# Fully deterministic — no API key required
WAGGLE_MODEL=deterministic python -m waggle.token_efficiency_benchmark
```

The harness generates a synthetic corpus of 10-domain conversations (~50
sessions, ~90 noise turns each), builds both a Waggle graph and a RAG chunk
index from the same data, then runs 30 queries across three categories —
single-fact retrieval, multi-hop reasoning, and extraction-failure recovery.
Results include token counts, percentage reduction, and recall@K, printed to
stdout and optionally saved to `results/`.

---

## Summary

```text
Without Waggle                        With Waggle
────────────────────────────────────  ────────────────────────────────────
Prompt size grows every turn          Prompt size stays within budget
Same fact re-injected each turn       Same fact stored once, retrieved once
Old context lost to truncation        Graph retains all facts indefinitely
Cost scales with conversation length  Cost is constant per turn
95–97% of context tokens redundant    Only relevant nodes injected
```

---

## Further Reading

| Topic | Source |
|---|---|
| Hybrid retrieval and RRF fusion | [`src/waggle/retrieval/hybrid.py`](../src/waggle/retrieval/hybrid.py) |
| Recursive context assembly and budgeting | [`src/waggle/recursive_context.py`](../src/waggle/recursive_context.py) |
| Token efficiency benchmark harness | [`src/waggle/token_efficiency_benchmark.py`](../src/waggle/token_efficiency_benchmark.py) |
| Graph observation and extraction flow | [`src/waggle/graph.py`](../src/waggle/graph.py) |
| Token estimation implementation | [`src/waggle/context_bundle.py`](../src/waggle/context_bundle.py#L24) |
