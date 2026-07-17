# Post-Heldout Validation Options Status

Date: 2026-07-17

This note records the checks requested after the 71-case LongMemEval-S blind run and the post-heldout context fixes. The 71-case slice remains spent and must not be used to tune or re-score headline results.

## Fresh Data Check

### LongMemEval-S

Local LongMemEval-S is effectively exhausted for blind validation.

- Local dataset: `benchmarks/longmemeval/longmemeval_s_cleaned.json`
- Total cases: 500
- Estimated spent or inspected cases: 484
- Estimated remaining never-inspected cases: 16
- Remaining by category: KU 4, SSU 5, MS 6, TR 1

The remaining 16 can only support a small directional check. TR at n=1 is not meaningful validation.

### LongMemEval-M

The cleaned Hugging Face dataset exposes a real `longmemeval_m_cleaned` split upstream.

- Dataset: `xiaowu0162/longmemeval-cleaned`
- File: `longmemeval_m_cleaned.json`
- Reported raw file size: 2,737,100,077 bytes
- Local status: not downloaded into this repo

This is the best candidate for a genuinely fresh LongMemEval evaluation, but it is about 2.74 GB and should be downloaded/prepared deliberately rather than committed to the repository.

### Synthetic Stress Tests

No runnable 90-case stress-test dataset is present locally.

Only the plan references the intended categories:

- `adversarial_contradictions`
- `cross_session_chains`
- `agent_decision_memory`

The stress suite still needs to be authored or restored before it can be used as validation data.

## Known-Failure Reader/Judge Validation

A mock split was created for already-inspected failure cases:

- `runs/longmemeval/waggle-stratified-24/fix-validation/known-failure-validation-split-plan.json`

The attempted MiniLM-backed reader+judge run did not complete. The process stalled before writing any rows. Separate probes showed the local project venv Python executables currently hang even on trivial startup, and MiniLM/SentenceTransformers initialization is therefore blocked in this environment.

Status:

- Static context validation: complete.
- Reader+judge confirmation on known failures: still open.
- Cause: local venv/runtime stall, not Groq quota.

The static validation already confirmed that the repaired contexts include the target evidence for:

- `8752c811`: `Sound effects`, `ambient`, `diegetic`
- `f523d9fe`: `Doc Martin`
- `dfde3500`: `Wednesday`

But these are not yet confirmed reader+judge flips.

## Second-Judge Matrix

Qwen re-judged all 24 answers from the 12 Llama disagreement cases independently. The adjusted number is not directionally selective; it replaces the labels for the disagreement payload only.

Llama-to-Qwen score matrix across those 24 answers:

| Llama | Qwen | Count |
| --- | --- | ---: |
| 0 | 0 | 9 |
| 0 | 1 | 3 |
| 1 | 0 | 1 |
| 1 | 1 | 11 |

By condition:

| Condition | Llama 0 / Qwen 0 | Llama 0 / Qwen 1 | Llama 1 / Qwen 0 | Llama 1 / Qwen 1 |
| --- | ---: | ---: | ---: | ---: |
| flat_vector | 4 | 0 | 0 | 8 |
| waggle_full | 5 | 3 | 1 | 3 |

Qwen-adjusted heldout-71 totals, replacing only disagreement-case labels:

- `flat_vector`: 55 / 71
- `waggle_full`: 53 / 71

## Next Valid Step

Use LongMemEval-M as the primary fresh validation source if disk/time budget allows. Otherwise, run the remaining 16 LongMemEval-S cases only as an explicitly underpowered directional check and label it as such.

Before any new blind run, unblock the local venv/runtime issue and complete reader+judge validation of the known-failure mock split.
