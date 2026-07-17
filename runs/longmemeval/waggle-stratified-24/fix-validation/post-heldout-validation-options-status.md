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
- `runs/longmemeval/waggle-stratified-24/fix-validation/known-failure-validation-7401057b-split-plan.json`

The initial MiniLM-backed reader+judge run did not complete. The process stalled before writing any rows.

Follow-up diagnosis separated two issues:

1. `.runtime-build-venv/bin/python` hangs even before site initialization and should not be used for this validation run.
2. System Python 3.11 can use `.runtime-build-venv` site-packages, but Transformers/SentenceTransformers startup was blocked by a slow `importlib.metadata.packages_distributions()` scan.

The runner now patches that Transformers distribution scan before embedding model construction. With:

```bash
PYTHONPATH=/Users/abhigyanshekhar/Desktop/MCP/src:/Users/abhigyanshekhar/Desktop/MCP/scripts:/Users/abhigyanshekhar/Desktop/MCP/.runtime-build-venv/lib/python3.11/site-packages \
/opt/homebrew/bin/python3.11 -u scripts/run_longmemeval_waggle_phase.py ...
```

MiniLM loads and reaches the reader call.

The current blocker for completing the remaining known-failure validation is Groq daily token quota for `llama-3.3-70b-versatile`, not local runtime. The attempted continuation reached `_groq_answer()` and received:

```text
Rate limit reached for model `llama-3.3-70b-versatile` ... tokens per day (TPD): Limit 100000, Used ~95194, Requested 6621. Please try again in ~26m.
```

Status:

- Static context validation: complete.
- MiniLM runtime/import path: fixed for the runner via system Python 3.11 plus metadata-scan patch.
- Reader+judge confirmation on known failures: still open.
- Current cause: Groq `llama-3.3-70b-versatile` TPD quota, not local runtime.

Completed reader+judge output:

- `runs/longmemeval/waggle-stratified-24/fix-validation/known-failure-validation-waggle-llama70b-postheldout-runtimefix.jsonl`
- `runs/longmemeval/waggle-stratified-24/fix-validation/known-failure-validation-0db4c65d-waggle-llama70b-runtimefix.jsonl`
- Completed rows: 8 / 8
- Total cost recorded for completed rows: about `$0.035`

Known-failure validation outcomes so far:

| Case | Category | Purpose | Judge | Outcome |
| --- | --- | --- | ---: | --- |
| `7401057b` | KU | Hilton recency/free-night count | 0 | Not fixed; model still answered single free night instead of two. |
| `73d42213` | MS | clinic arrival time | 1 | Fixed end-to-end. |
| `8752c811` | SSA | 27th prompt parameter, Sound effects | 1 | Fixed end-to-end. |
| `f523d9fe` | SSA | Netflix show, Doc Martin | 1 | Fixed end-to-end. |
| `dfde3500` | KU | previous tutor day, Wednesday | 1 | Fixed end-to-end. |
| `a82c026e` | SSU | game specificity control | 0 | Still not fixed; answer omitted `DLC`. |
| `0bb5a684` | TR | temporal date-anchor control | 1 | Fixed/end-to-end correct. |
| `0db4c65d` | TR | temporal date-anchor control | 1 | Fixed/end-to-end correct. |

The precise-source fixes are now validated end-to-end on the three intended cases. The TR date-anchor controls both pass. The recency-resolution fix is only partially validated: `dfde3500` passes, but `7401057b` still fails, so recency should not be described as solved.

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
