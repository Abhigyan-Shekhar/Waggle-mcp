# Temporal Slot Retrieval

Waggle now has the write-time foundation for relation-aware temporal memory.
The original transcript and graph nodes remain immutable evidence; a compact
`fact_heads` projection identifies the current version of high-confidence state
claims.

## Implemented

- `FactKind` distinguishes state snapshots from sets, events, preferences,
  derived values, and uncertain open-world claims.
- `NormalizedClaim` carries subject, relation, normalized value, scope,
  effective time, observed time, confidence, and source provenance.
- `fact_heads` maps `(tenant, subject, relation, scope)` to one current state
  node while the `nodes` table remains the version ledger.
- Newer `state_single` and `state_snapshot` claims close the previous node's
  validity interval and create an `UPDATES` edge.
- Backfilled older state is stored as history and cannot replace a newer head.
- `event`, `state_set`, `preference`, `derived`, and `open_world` claims remain
  append-only.
- Claims below `0.85` confidence remain append-only.
- `get_state_fact(..., as_of=...)` supports direct current and historical lookup.
- Hybrid node retrieval filters invalidated versions before vector/BM25 fusion
  and graph expansion.
- Graph expansion now follows the typed evidence relations used by Waggle,
  rather than only `DERIVED_FROM`.

## Conservative ingestion classification

The existing `observe_conversation` extraction path now attaches normalized
claim semantics only for explicit patterns with clear storage behavior:

- user-stated residence/location: `state_single`
- explicit count snapshots: `state_snapshot`
- project choices and selected configuration values: state
- purchases, additions, attendance, starts/finishes, returns, and exchanges:
  `event`
- preferences: append-only `preference`

Assistant-authored and ambiguous claims do not receive enough confidence to
retire state. This avoids laundering speculation into a current fact.

## Slot retrieval implemented

- A deterministic planner recognizes current, historical, aggregation,
  comparison, temporal-difference, enumeration, preference, and direct queries.
- Every required operand receives an independent retrieval query and reserved
  evidence capacity.
- Selection deduplicates repeated evidence within each slot and favors source
  diversity instead of filling a slot with paraphrases of one memory.
- Local calculations support conservative sums, differences, percentages, date
  differences, and clock-time offsets. Calculation abstains when operands are
  missing or ambiguous.
- Relative dates (`today`, `yesterday`, and one-to-seven days ago) are resolved
  against the evidence timestamp; an explicit date in the question can serve as
  the comparison anchor.
- The compact compiler emits evidence by slot, verified calculations, and an
  explicit missing-evidence section under a hard context budget.
- The additive public path is
  `graph.temporal_slot_retriever().retrieve(...)`; legacy `graph.query()` is
  unchanged while this path is evaluated.

## Temporal Slot v2 contracts

The compact path now preserves answer-bearing structure instead of reducing
every retrieval hit to an interchangeable text span:

- `list_item` atoms retain the list title, requested index, exact value, and
  adjacent items.
- `table_cell` atoms cannot be emitted without row, column, and cell labels.
- `assistant_answer` atoms require evidence from an assistant turn.
- `preference` atoms carry grounding (`explicit`, `history`, or `inferred`),
  query scope, and a deterministic scope-compatibility check.
- `state_set` atoms are materialized as canonical active members after removal
  events are applied.
- arithmetic and date operations are withheld unless every required slot has
  an unambiguous operand.
- Derived arrival/departure questions reserve independent clock-time and travel-
  duration slots across sessions. Exact clock arithmetic is compiled only when
  both focused evidence atoms satisfy their slot-specific answer shapes; a
  clock elsewhere in an oversized retrieval hit cannot validate a clock-free
  focused snippet.

Before compilation, `EvidenceValidator` checks required slots, roles,
structural fields, operation completeness, active-state ambiguity, active-set
enumeration, and preference scope. A failed contract permits exactly one
targeted retrieval expansion for each affected slot (`top_k=40` after the
normal `top_k=20` pass). The second result is validated again; unresolved
issues are shown explicitly to the reader instead of silently presenting an
incomplete problem.

Current-set queries always receive the bounded enumeration pass because a
single retrieved member cannot establish open-world completeness. This is the
only query class that expands proactively. The default `1200`-token compiler
budget is unchanged.

Synthetic fixtures cover indexed lists, table schemas, assistant-origin
answers, two-operand differences, multi-event totals, active-set additions and
cancellations, preference scope, date calculations, forward/reverse clock-time
offsets, unrelated-time distractors, and missing-operand fallback. Frozen
benchmark slices remain immutable and are not rescored while v2 is developed.

## Remaining phases

1. Extend deterministic set-member extraction beyond current subscriptions to
   general `SET_UNION` queries and richer add/remove language.
2. Extend temporal normalization to weeks, months, and calendar expressions.
3. Map direct current/historical questions to normalized relation keys so they
   can bypass similarity retrieval through `get_state_fact`.
4. Add ontology-backed preference-domain aliases; v2 intentionally uses a
   conservative lexical compatibility gate.
5. Evaluate the frozen v2 implementation on a completely new slice or
   LongMemEval-M before making it the production default.

The next benchmark gate is state freshness and event preservation. Slot-based
aggregation should be introduced only after these invariants remain stable on
the existing temporal and LongMemEval regression suites.
