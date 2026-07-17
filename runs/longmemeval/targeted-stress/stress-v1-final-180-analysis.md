# Targeted Stress v1 Final Analysis

## Status

The targeted stress v1 run is complete.

- Cases: `90`
- Rows: `180`
- Conditions: `flat_vector`, `waggle_full`
- Suite: `supplementary_stress`
- Official LongMemEval-S eligible: `false`
- Dataset SHA-256: `9ac9bc955b9fa3029c6f19aa35c6a8bcf374970009d195308ad4f257318e972a`
- Result artifact: `runs/longmemeval/targeted-stress/stress-v1-final-180.jsonl`
- Summary JSON: `runs/longmemeval/targeted-stress/stress-v1-final-180-summary.json`
- Summary Markdown: `runs/longmemeval/targeted-stress/stress-v1-final-180-summary.md`

## Final Score

| condition | score | exact support coverage | context tokens | input tokens | output tokens | cost |
|---|---:|---:|---:|---:|---:|---:|
| `flat_vector` | 88/90 | 90/90 | 18859 | 53159 | 7570 | $0.049964 |
| `waggle_full` | 88/90 | 90/90 | 36208 | 71456 | 6992 | $0.059964 |

Waggle used `1.92x` the context tokens of flat-vector retrieval on this suite.

## Category Breakdown

| category | flat_vector | waggle_full |
|---|---:|---:|
| `adversarial_contradictions` | 28/30 | 28/30 |
| `cross_session_chains` | 30/30 | 30/30 |
| `agent_decision_memory` | 30/30 | 30/30 |

## Mechanism Breakdown

| mechanism | flat_vector | waggle_full |
|---|---:|---:|
| `assistant_inference_vs_user_constraint` | 8/10 | 8/10 |
| `source_authority_user_aside` | 10/10 | 10/10 |
| `direct_user_correction` | 10/10 | 10/10 |
| `two_session_temporal_arithmetic` | 10/10 | 10/10 |
| `multi_session_count_chain` | 10/10 | 10/10 |
| `decision_dependency_chain` | 10/10 | 10/10 |
| `decision_plus_rationale` | 10/10 | 10/10 |
| `exact_compound_answer_fidelity` | 10/10 | 10/10 |
| `project_constraint_update` | 10/10 | 10/10 |

## Disagreements

There were two flat-vs-Waggle disagreements, both in
`assistant_inference_vs_user_constraint`:

| case | flat_vector | waggle_full |
|---|---:|---:|
| `stress_ac_ai_01_dinner_constraint` | 0 | 1 |
| `stress_ac_ai_09_meal_timing` | 1 | 0 |

The disagreements cancel out in the aggregate result.

## Interpretation

This targeted stress suite does not show a Waggle QA advantage over the matched
flat-vector baseline. Both systems retrieve all gold support and both score
`88/90` under the same reader and same judge.

The useful finding is narrower:

- The stress harness works end to end and keeps supplementary rows separate
  from official LongMemEval-S results.
- The current prompt/harness handles most designed source-authority,
  cross-session, and agent-decision stress cases for both systems.
- Waggle reaches parity but injects substantially more context.
- The remaining observed weakness is concentrated in
  `assistant_inference_vs_user_constraint`, where both systems score `8/10`.

Before using this in a paper, the two failed mechanism rows and the two
disagreement rows should be checked with an independent judge because the
current run uses `llama-3.3-70b-versatile` as both reader and judge.
