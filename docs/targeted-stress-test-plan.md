# Targeted Stress-Test Plan

## Purpose

Lisa recommended the targeted stress test over LongMemEval-M because it probes
the failure mechanisms found in the blind LongMemEval-S run more directly. This
suite is supplementary evidence for the systems paper, not an official
LongMemEval-S result.

Stress-test rows must use:

- `suite`: `supplementary_stress`
- `split`: `stress`
- `official_table_eligible`: `false`

They must never be merged into the official LongMemEval-S table.

## What The Suite Tests

The stress suite targets three categories already supported by the result
validator:

- `adversarial_contradictions`: source-authority conflicts where an older,
  assistant-inferred, or tentative value competes with a later direct user
  statement.
- `cross_session_chains`: questions that require combining evidence from two
  or more independent sessions, especially where graph traversal can over-focus
  on one local neighborhood.
- `agent_decision_memory`: project decisions, constraints, rationales, exact
  feature names, and later updates that should be preserved as durable memory.

These categories map to the two named residual failure modes from the
LongMemEval-S work:

- Source-authority resolution: direct user statements should outrank assistant
  guesses, hedged summaries, and stale values.
- Exact-answer fidelity: compound terms, enumerated items, and decision
  rationales should not be shortened or paraphrased when the question asks for
  the exact value.

## Case Plan

The full stress suite target is 90 cases:

- 30 `adversarial_contradictions`
- 30 `cross_session_chains`
- 30 `agent_decision_memory`

The first committed artifact is a 9-case seed suite, three cases per category,
for smoke testing the harness and prompt behavior before authoring all 90 cases.

Each case stores:

- `case_id`
- `stress_category`
- `mechanism`
- `question_type` for prompt routing through the existing LongMemEval-style
  harness
- `question`
- `question_date`
- `answer`
- `answer_session_ids`
- `gold_evidence`
- `expected_failure_mode`
- `haystack_session_ids`
- `haystack_dates`
- `haystack_sessions`

## Evaluation Protocol

Run the same conditions used in the LongMemEval-S harness:

- `flat_vector`
- `waggle_full`

Use the same embedding model and chunking policy for both conditions. The only
intended variable is whether Waggle's typed memory graph and context assembly
help or hurt on the stress mechanism.

Primary metrics:

- judged QA accuracy by stress category
- exact support coverage
- retrieved support IDs
- context tokens
- input/output tokens
- latency
- cost

Secondary diagnostics:

- whether the correct evidence is present in the final context
- whether the correct evidence is near the start/end or buried mid-context
- whether structured memory crowds out raw source chunks
- whether the reader follows direct user-stated facts over assistant-inferred
  facts

## Execution Order

1. Generate and inspect the 9-case seed suite.
2. Run the seed suite through `flat_vector` and `waggle_full`.
3. Confirm rows validate as `supplementary_stress` with
   `official_table_eligible=false`.
4. If the seed suite behaves as expected, author the remaining cases to reach
   90 total.
5. Freeze the harness commit before the full 90-case run.
6. Run the full stress suite uninterrupted and report it separately from
   LongMemEval-S.

## Current Artifacts

- Dataset generator: `scripts/create_targeted_stress_seed.py`
- Seed dataset: `benchmarks/longmemeval/targeted_stress_seed.json`
- Seed split plan: `runs/longmemeval/targeted-stress/split-plan-stress-seed.json`

## Open Work

- Run the 9-case seed suite with `flat_vector` and `waggle_full`.
- Add a second judge model for disagreement checks if the result is close.
- Expand the suite from 9 to 90 cases after seed-suite validation.
- Add a short paper-methods note that the stress suite was chosen after
  LongMemEval-S exposed source-authority and exact-answer-fidelity failures.
