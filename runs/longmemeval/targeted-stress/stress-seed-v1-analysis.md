# Targeted Stress Seed v1 Analysis

## Run

- Dataset: `benchmarks/longmemeval/targeted_stress_seed.json`
- Dataset SHA-256: `9b90f1ddc5b35719abb1ecbf86661b09bb4319fb3a989b85c789ecc7c4737f0a`
- Split plan: `runs/longmemeval/targeted-stress/split-plan-stress-seed.json`
- Output: `runs/longmemeval/targeted-stress/stress-seed-v1-llama33-flat-vs-waggle.jsonl`
- Summary JSON: `runs/longmemeval/targeted-stress/stress-seed-v1-summary.json`
- Summary Markdown: `runs/longmemeval/targeted-stress/stress-seed-v1-summary.md`
- Suite: `supplementary_stress`
- Split: `stress`
- Conditions: `flat_vector`, `waggle_full`
- Reader: `llama-3.3-70b-versatile`
- Judge: `llama-3.3-70b-versatile`
- Prompt version: `longmemeval-systems-v1-stress-seed-v1`

## Score

Raw judge score:

| condition | score | exact support coverage | context tokens | input tokens | output tokens | cost |
|---|---:|---:|---:|---:|---:|---:|
| `flat_vector` | 8/9 | 9/9 | 1851 | 5093 | 650 | $0.004724 |
| `waggle_full` | 8/9 | 9/9 | 3743 | 6903 | 759 | $0.005943 |

By category:

| category | flat_vector | waggle_full |
|---|---:|---:|
| `adversarial_contradictions` | 2/3 | 2/3 |
| `cross_session_chains` | 3/3 | 3/3 |
| `agent_decision_memory` | 3/3 | 3/3 |

Total run cost: `$0.010667`.

## Main Finding

The 9-case seed suite is too easy for the current prompt and retrieval stack.
Both systems retrieved all gold support sessions and answered 8/9 by the raw
same-model judge. There were no flat-vs-Waggle disagreement cases.

The only scored failure is likely a judge false negative:

- Case: `stress_ac_003_assistant_hallucinated_preference`
- Gold answer: `gluten-free`
- Flat answer: says the dinner plan should respect a `gluten-free dietary constraint`.
- Waggle answer: says the dinner plan should respect a `gluten-free dietary constraint`.
- Llama judge label: `no` for both.

Because both answers contain the required dietary constraint and explain the
user correction, this row should be second-judged before treating the 8/9 score
as a real miss.

## Context Budget

Waggle used more context than flat on this seed:

- Flat total context tokens: `1851`
- Waggle total context tokens: `3743`
- Waggle/flat context ratio: `2.02x`

That matches the LongMemEval-S pattern: Waggle's structured-memory context can
carry more information but also increases the reader's token burden.

## Implication

This seed run validates the harness and artifact format, but it does not yet
stress the systems enough to distinguish them. The full 90-case stress suite
should make cases harder in three ways:

1. Add more distractor sessions per case, especially semantically adjacent
   distractors.
2. Add cases where the correct answer is present but not in the most similar
   session.
3. Add exact-answer cases where dropping one qualifier makes the answer wrong.

## Next Step

Expand from the 9-case smoke suite to the full 90-case targeted stress suite:

- 30 `adversarial_contradictions`
- 30 `cross_session_chains`
- 30 `agent_decision_memory`

Before the full run, add a second judge pass for `stress_ac_003` or replace the
same-model judge with an independent judge for the stress suite.
