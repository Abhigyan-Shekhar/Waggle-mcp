# Targeted Stress v1 Current 176-Row Status

## Status

The targeted stress v1 run has completed `176/180` rows:

- Complete case pairs: `88/90`
- Completed rows: `176/180`
- Missing cases:
  - `stress_agent_cu_09_repo_collab`
  - `stress_agent_cu_10_dataset_download`

Both missing cases are in `agent_decision_memory` / `project_constraint_update`.
They were not scored because Groq hit the daily token limit on the second API
key before those rows could run.

## Current Validated Scores

These numbers are still incomplete and should not be reported as the final
90-case stress-suite result.

| condition | rows | score | exact support coverage | context tokens | cost |
|---|---:|---:|---:|---:|---:|
| `flat_vector` | 88 | 86/88 | 88/88 | 18463 | $0.048636 |
| `waggle_full` | 88 | 86/88 | 88/88 | 35683 | $0.058917 |

By category:

| category | flat_vector | waggle_full |
|---|---:|---:|
| `adversarial_contradictions` | 28/30 | 28/30 |
| `cross_session_chains` | 30/30 | 30/30 |
| `agent_decision_memory` | 28/28 | 28/28 |

## Interpretation

The current 176-row result shows parity between `flat_vector` and `waggle_full`
on judged QA, with perfect support coverage for both. Waggle uses about `1.93x`
the context tokens in the completed rows.

This remains a partial result. The two missing cases are already isolated in
resume split plans and should be run before reporting a final stress-suite
number.

## Remaining Resume Inputs

Run the remaining two cases with:

- `runs/longmemeval/targeted-stress/split-plan-stress-v1-final-both.json`

Optional one-case splits also exist:

- `runs/longmemeval/targeted-stress/split-plan-stress-v1-final-stress_agent_cu_09_repo_collab.json`
- `runs/longmemeval/targeted-stress/split-plan-stress-v1-final-stress_agent_cu_10_dataset_download.json`
