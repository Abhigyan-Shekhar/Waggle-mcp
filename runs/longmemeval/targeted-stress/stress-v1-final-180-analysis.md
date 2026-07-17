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
- The only apparent Llama-judge weakness is concentrated in
  `assistant_inference_vs_user_constraint`, where both systems score `8/10`
  before the targeted second-judge check below.

Before using this in a paper, the two failed mechanism rows and the two
disagreement rows should be checked with an independent judge because the
current run uses `llama-3.3-70b-versatile` as both reader and judge.

## Post-Run Audit

### Context Scale

The context-token counts above are totals, not averages. Per case:

- `flat_vector`: `209.5` context tokens on average.
- `waggle_full`: `402.3` context tokens on average.

This is much smaller than the real LongMemEval-S held-out shards run earlier,
where checked local artifacts were roughly:

- `flat_vector`: `1373-2128` context tokens per case.
- `waggle_full`: `4977-5432` context tokens per case.

The generated stress dataset is also much smaller at the source level:

- targeted stress v1: `3.78` sessions per case on average, about `100`
  source tokens per case.
- LongMemEval-S: `47.7` sessions per case on average, about `128k` source
  tokens per case before retrieval.

So this suite should be described narrowly: it stress-tests answer synthesis
over small, controlled conflict patterns. It does not test retrieval robustness
at LongMemEval haystack scale.

### Case Generation Method

The 90 stress cases were deterministically generated from hand-written Python
templates in `scripts/create_targeted_stress_full.py`. They were not generated
end-to-end by the reader model during evaluation.

This still makes the suite a self-authored diagnostic extension, not an
independent benchmark. It should be treated as supplementary mechanism evidence.

### Gold-Answer Spot Check

A static audit found that `70/90` answers appear literally in the marked gold
support sessions. The `20/90` non-literal cases are the intended arithmetic
cases (`two_session_temporal_arithmetic` and `multi_session_count_chain`), where
the answer is derived from the support rather than copied verbatim.

Manual spot checks across representative categories confirmed that the marked
gold sessions contain the intended evidence for:

- source-authority aside conflicts
- multi-session count chains
- exact compound feature names
- project constraint updates

### Second Judge Check

The apparent weakness in `assistant_inference_vs_user_constraint` did not hold
up under targeted second judging. The six key rows from the two disagreement
cases and the both-failed gift-preference case were rejudged with
`qwen/qwen3-32b`.

Qwen marked all six rows correct:

- `stress_ac_ai_01_dinner_constraint`: both conditions correct.
- `stress_ac_ai_04_gift_preference`: both conditions correct.
- `stress_ac_ai_09_meal_timing`: both conditions correct.

With this targeted second-judge correction:

| condition | Llama judge | Qwen-targeted adjusted |
|---|---:|---:|
| `flat_vector` | 88/90 | 90/90 |
| `waggle_full` | 88/90 | 90/90 |

This means the `assistant_inference_vs_user_constraint` misses are better
described as Llama-judge false negatives than confirmed source-authority
failures. The stress suite still shows parity, but after second judging it no
longer shows a replicated failure mechanism.

This does not retract the real LongMemEval-S source-authority failure observed
in `7401057b`. In that case, Waggle retrieved the relevant support but answered
with the older assistant-inferred single-night Hilton value instead of the later
user-stated two-night value. The narrower conclusion is that the synthetic
stress suite did not replicate that real failure under second judging. The most
likely explanation is that these synthetic source-authority cases are too small
and direct: the corrected user value is short, explicit, and close to the query,
whereas `7401057b` occurred inside a much larger context with older evidence
appearing earlier and the correct two-night evidence appearing mid-context.

### Judge Bias Pattern

Across Qwen adjudications run so far:

- LongMemEval-S disagreement rows: `24` rejudged rows.
- Targeted stress key rows: `6` rejudged rows.
- Total: `30` rejudged rows.

Flip direction:

| flip type | count |
|---|---:|
| Llama `no` -> Qwen `yes` | 7 |
| Llama `yes` -> Qwen `no` | 1 |
| agree `yes` | 13 |
| agree `no` | 9 |

This suggests the Llama judge is more false-negative-prone than false-positive
prone on the inspected rows. Any Llama-only score should therefore be treated as
a conservative estimate until independently adjudicated.
