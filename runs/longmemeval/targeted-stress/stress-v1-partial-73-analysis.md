# Targeted Stress v1 Partial Run Analysis

## Status

The full 90-case targeted stress run was started but did not complete because
Groq hit the daily token limit for `llama-3.3-70b-versatile`.

Completed artifact:

- Output: `runs/longmemeval/targeted-stress/stress-v1-llama33-flat-vs-waggle.jsonl`
- Rows completed: `73/180`
- Complete case pairs: `36/90`
- Partial case: `stress_chain_tr_07_course_start`
  - `flat_vector`: complete
  - `waggle_full`: missing
- Remaining untouched case pairs: `53`

The failure happened during the next reader call after the completed
`flat_vector` row for `stress_chain_tr_07_course_start`.

## Partial Scores

These numbers are diagnostic only because the run is incomplete.

| condition | rows | score | exact support coverage | context tokens | cost |
|---|---:|---:|---:|---:|---:|
| `flat_vector` | 37 | 35/37 | 37/37 | 7923 | $0.021197 |
| `waggle_full` | 36 | 34/36 | 36/36 | 15515 | $0.025332 |

By completed rows:

| category | flat_vector | waggle_full |
|---|---:|---:|
| `adversarial_contradictions` | 28/30 | 28/30 |
| `cross_session_chains` | 7/7 | 6/6 |

No `agent_decision_memory` rows have run yet.

## Resume Artifacts

Two resume split plans were created so the run can continue without duplicating
completed rows:

- `runs/longmemeval/targeted-stress/split-plan-stress-v1-resume-waggle-only.json`
  - one case: `stress_chain_tr_07_course_start`
  - run with `--condition waggle_full`
- `runs/longmemeval/targeted-stress/split-plan-stress-v1-resume-both.json`
  - 53 cases
  - run with `--condition flat_vector --condition waggle_full`

The resumed outputs should be written to separate shard files and merged only
after validation.

## Resume Commands

Missing Waggle-only row:

```bash
GROQ_API_KEY=... GROQ_MAX_TOKENS=300 .runtime-build-venv/bin/python \
  scripts/run_longmemeval_waggle_phase.py \
  benchmarks/longmemeval/targeted_stress_v1.json \
  --split-plan runs/longmemeval/targeted-stress/split-plan-stress-v1-resume-waggle-only.json \
  --output runs/longmemeval/targeted-stress/stress-v1-resume-waggle-only.jsonl \
  --split stress \
  --suite supplementary_stress \
  --condition waggle_full \
  --reader-model llama-3.3-70b-versatile \
  --judge-model llama-3.3-70b-versatile \
  --prompt-version longmemeval-systems-v1-stress-v1 \
  --retrieval-limit 5
```

Remaining full case pairs:

```bash
GROQ_API_KEY=... GROQ_MAX_TOKENS=300 .runtime-build-venv/bin/python \
  scripts/run_longmemeval_waggle_phase.py \
  benchmarks/longmemeval/targeted_stress_v1.json \
  --split-plan runs/longmemeval/targeted-stress/split-plan-stress-v1-resume-both.json \
  --output runs/longmemeval/targeted-stress/stress-v1-resume-both.jsonl \
  --split stress \
  --suite supplementary_stress \
  --condition flat_vector \
  --condition waggle_full \
  --reader-model llama-3.3-70b-versatile \
  --judge-model llama-3.3-70b-versatile \
  --prompt-version longmemeval-systems-v1-stress-v1 \
  --retrieval-limit 5
```

## Interpretation

Do not report the partial score as the stress-suite result. It is useful only
as a progress artifact and as evidence that the generated suite runs through
the same validated `supplementary_stress` artifact path.
