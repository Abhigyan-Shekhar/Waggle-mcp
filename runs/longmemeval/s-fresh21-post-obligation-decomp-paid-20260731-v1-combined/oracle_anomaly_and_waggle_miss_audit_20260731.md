# Oracle Anomaly and Waggle Miss Audit — 2026-07-31

Run: `s-fresh21-post-obligation-decomp-paid-20260731-v1-combined`

## Corrected Scores After Existing Manual Audit

| condition | corrected score | avg context tokens |
|---|---:|---:|
| flat_transcript_vector | 12/21 | 2501.8 |
| waggle_production_context | 15/21 | 2748.5 |
| oracle_support_context | 11/21 | 3369.0 |

One oracle row was manually corrected before this audit: `38146c39 / oracle_support_context` should be correct because it grounds cookie advice in turbinado sugar.

## Main Finding

The oracle-below-flat anomaly is real, but it does **not** mean Waggle's 15/21 is automatically invalid. It means the current `oracle_support_context` condition is not a clean ceiling. It appears to provide gold **sessions** as long raw transcript slices, not compact gold **answer-bearing turns**. Because the oracle context is token-heavy and ordered as raw session text, decisive evidence is often absent from the final context or buried behind irrelevant support turns.

This makes oracle useful as a diagnostic baseline for raw-support formatting, but not a trustworthy upper bound unless rebuilt as exact answer-turn oracle.

## Oracle Miss Classification

| case_id | category | oracle status | classification | evidence |
|---|---|---|---|---|
| f685340e | KU | wrong | oracle-context missing/recency evidence incomplete | Correct answer needs previous weekly tennis and current every-other-week tennis. Oracle context contains every-other-week but not the weekly previous state. |
| 3ba21379 | KU | wrong | oracle-context truncation/order failure | Gold session contains Ford F-150 pickup truck, but oracle context contains Mustang and not F-150/pickup truck. Waggle answers correctly. |
| gpt4_ab202e7f | MS | wrong | oracle-context incomplete multi-session evidence | Gold requires five kitchen items. Oracle answer sees only toaster. Frozen gold sessions contain faucet, mat, toaster, coffee maker, shelves, but oracle context does not surface all. |
| 157a136e | MS | wrong | reader failure over present evidence | Oracle context contains 75 and 32, but reader refuses to compute 75 - 32. Waggle answers correctly. |
| 58470ed2 | SSA | wrong | oracle-context truncation failure | Frozen gold session contains the Borges sphere/center/hexagons/circumference quote. Oracle context lacks those terms entirely. |
| 0862e8bf_abs | SSU | wrong | reader/task failure under oracle format | Gold asks hamster name; correct response is no hamster mentioned, cat Luna mentioned. Oracle context contains Luna but no hamster, and reader incorrectly answers cat name. Flat and Waggle answer correctly. |
| gpt4_e061b84g | TR | wrong | oracle-context wrong competing event surfaced | Gold is company charity soccer tournament. Oracle context contains Midsummer 5K and not charity soccer. Waggle answers correctly. |
| gpt4_468eb063 | TR | wrong | temporal reader failure / weak date normalization | Oracle context contains Emma and last-week context but answer says date cannot be determined. Waggle gives 7 days, still wrong vs 9/10. |
| gpt4_b4a80587 | TR | wrong | oracle-context missing second event | Gold requires comparing road trip and prime lens. Oracle context has road trip, not prime lens. All conditions answer road trip first, wrong. |
| b9cfe692 | TR | wrong | oracle-context missing numeric duration | Gold requires 2.5 weeks + 3 weeks. Oracle context has book titles but not 2.5, while flat and Waggle answer correctly. |

## Waggle Miss Classification

| case_id | category | flat | oracle | classification | implication |
|---|---|---:|---:|---|---|
| f685340e | KU | wrong | wrong | source/recency coverage unresolved | All conditions fail; do not attribute specifically to Waggle. Needs exact previous/current update extraction. |
| gpt4_ab202e7f | MS | wrong | wrong | multi-item aggregation failure | All conditions fail; current context does not force exhaustive counting across sessions. |
| 1f2b8d4f | MS | correct | correct-ish | Waggle assembly/retrieval miss | Waggle misses the budget-store $50 pair; flat gets exact $750. This is a real Waggle repair target. |
| 58470ed2 | SSA | wrong | wrong | support/evidence missing in all contexts | Frozen support has quote but all packed contexts miss it. This is support-turn centering, not Waggle-only. |
| gpt4_468eb063 | TR | wrong | wrong | temporal normalization failure | Waggle finds Emma context but computes 7 instead of 9/10 days. Reader/date arithmetic issue. |
| gpt4_b4a80587 | TR | wrong | wrong | temporal ordering/source ambiguity | All conditions say road trip first; gold says prime lens first. Needs explicit event-date extraction, not generic context. |

## What Remains True

- Waggle production context still beats flat on this fresh21 slice: **15/21 vs 12/21 corrected**.
- Token tax is modest after the context-assembly fixes: Waggle averages **2748.5** tokens vs flat **2501.8**, not the earlier ~2x bloat.
- Oracle should not be used as a clean ceiling until rebuilt. Current oracle is a raw gold-session pack and often loses answer-bearing turns.
- The strongest current repair targets are not generic context ordering anymore. They are:
  1. exact answer-turn centering for oracle/support-style contexts,
  2. multi-item aggregation across sessions,
  3. temporal date/event extraction,
  4. one Waggle-specific price-difference retrieval/assembly miss (`1f2b8d4f`).

## Recommended Next Engineering Step

Before any new paid run, rebuild `oracle_support_context` into `oracle_answer_turn_context`: include the actual `has_answer=True` turns, centered snippets around answer-like terms, and enough surrounding turn context, rather than raw session prefixes. Then re-score only oracle on this spent slice for diagnostic calibration. This does not change the Waggle-vs-flat result, but it tells us whether oracle becomes a sensible ceiling.

## Follow-up Implementation — `oracle_answer_turn_context`

Added a new diagnostic condition, `oracle_answer_turn_context`, while leaving historical `oracle_support_context` unchanged.

Implementation behavior:

- Uses the same gold support IDs as the old oracle baseline.
- Centers turns marked `has_answer=True` inside those gold support sessions before applying the context budget.
- Includes immediate neighboring turns after the answer-bearing turn, not before it, so long prior turns cannot clip the answer.
- For long answer turns, emits a query-focused snippet around the most specific query term rather than preserving only the turn prefix.
- Falls back to query-centered support turns only when a gold support session has no `has_answer=True` marker.

Dry-run artifact:

- `runs/longmemeval/s-fresh21-oracle-answer-turn-dryrun-20260731-v2`

Mechanical coverage check on old oracle misses:

| case_id | old oracle issue | new oracle-answer-turn context |
|---|---|---|
| 3ba21379 | missed Ford F-150/pickup truck | now contains both |
| gpt4_ab202e7f | missed several kitchen items | now contains faucet, mat, toaster, coffee maker, shelves |
| 157a136e | contained 75 and 32 already | still contains both with fewer tokens |
| 58470ed2 | clipped Borges quote | now contains sphere, center, hexagons, circumference |
| gpt4_e061b84g | surfaced Midsummer 5K, missed charity soccer | now contains charity soccer |
| gpt4_b4a80587 | missed prime lens | now contains prime lens and road trip |
| b9cfe692 | missed 2.5-week duration | now contains two and a half, three weeks, both book names |
| f685340e | missing previous weekly state | still missing literal weekly previous-state evidence under term check; leave as dataset/evidence ambiguity until manually inspected deeper |

Focused tests added:

- `test_oracle_answer_turn_context_centers_late_has_answer_turn`
- `test_oracle_answer_turn_context_query_fallback_for_negative_answers`
- `test_oracle_answer_turn_context_focuses_inside_long_answer_turn`

Validation:

- `PYDANTIC_DISABLE_PLUGINS=1 .runtime-build-venv/bin/python -m pytest tests/test_longmemeval_full_conditions.py tests/test_recursive_context_evidence_priority.py`
- Result: `57 passed`

## TR Judge Audit Status

The temporal-reasoning classifications for `gpt4_468eb063` and `gpt4_b4a80587` were cross-judged with `qwen/qwen3.6-27b` because prior runs showed Llama judge errors concentrated in TR.

Artifacts:

- Payload: `qwen_temporal_audit_payload_20260731.jsonl`
- Results: `qwen36_temporal_audit_results_20260731_v2.jsonl`

Result: Qwen agreed with the primary judge on all six audited rows: flat, Waggle, and oracle are all incorrect for both temporal cases. These TR rows should remain classified as real temporal failures, not judge artifacts.

## Paid Diagnostic Score — `oracle_answer_turn_context`

After adding `oracle_answer_turn_context`, scored the same spent fresh21 slice as a diagnostic calibration run.

Artifact:

- `runs/longmemeval/s-fresh21-oracle-answer-turn-paid-20260731-v1`

Result:

| condition | score | avg context tokens | cost |
|---|---:|---:|---:|
| old oracle_support_context | 11/21 corrected | 3369.0 | previous run |
| new oracle_answer_turn_context | 19/21 raw | 856.1 | $0.012998 |

Category breakdown for `oracle_answer_turn_context`:

| category | score |
|---|---:|
| knowledge-update | 4/4 |
| multi-session | 4/4 |
| single-session-assistant | 4/4 |
| single-session-preference | 0/1 |
| single-session-user | 4/4 |
| temporal-reasoning | 3/4 |

Remaining misses:

- `38146c39` / SSP: answer gave a specific cookie ingredient (`ginger`) but did not ground the advice in the user's turbinado-sugar preference. This is an oracle-answer-turn reader/personalization failure, not an evidence-truncation failure.
- `gpt4_468eb063` / TR: answer said `0 days ago` because the answer turn says Emma was met “today”; gold expects 9/10 days relative to the question date. This remains a temporal normalization/date anchoring failure.

Conclusion: the oracle-below-flat anomaly is resolved. The old oracle baseline was invalid as a ceiling because it packed raw gold-session prefixes. The rebuilt answer-turn oracle behaves like a sensible diagnostic ceiling: 19/21, with two residual reader/task failures.

## Recomputed Waggle Repair Targets Against Rebuilt Oracle

After scoring `oracle_answer_turn_context`, the earlier statement that `1f2b8d4f` was the only clean Waggle-specific target is no longer valid. That conclusion depended on the broken old oracle. Recomputing against the rebuilt 19/21 oracle gives:

| group | count |
|---|---:|
| flat wrong, Waggle wrong, oracle wrong | 1 |
| flat wrong, Waggle wrong, oracle correct | 4 |
| flat wrong, Waggle correct, oracle wrong | 1 |
| flat wrong, Waggle correct, oracle correct | 3 |
| flat correct, Waggle wrong, oracle correct | 1 |
| flat correct, Waggle correct, oracle correct | 11 |

Oracle-solvable set:

- `oracle_answer_turn_context`: 19/21
- `waggle_production_context` on oracle-solvable rows: 14/19
- `flat_transcript_vector` on oracle-solvable rows: 12/19

Clean Waggle repair targets where rebuilt oracle is correct and Waggle is wrong:

| case_id | category | failure shape |
|---|---|---|
| f685340e | KU | previous/current update pair exists; Waggle surfaces only current every-other-week tennis and misses previous weekly tennis |
| gpt4_ab202e7f | MS | exhaustive multi-item aggregation across sessions; Waggle finds only kitchen mat/faucet/shelves ambiguity, misses full five-item set |
| 1f2b8d4f | MS | price-difference comparison; Waggle sees $800 and low-price suggestions but misses/does not use the exact $50 budget-store pair |
| 58470ed2 | SSA | exact quote retrieval/centering; Waggle says quote absent although answer-bearing support exists |
| gpt4_b4a80587 | TR | temporal ordering; Waggle compares transcript dates/relative phrases incorrectly and says road trip before lens, while answer-turn oracle resolves lens first |

Manual check on `f685340e`:

The rebuilt oracle context contains both answer-bearing turns:

- previous state: user says seeing tennis players reminded them of their own weekly tennis sessions with friends
- current state: user says they plan to play tennis this Sunday like they do every other week

The oracle answer says: “You used to play tennis with your friends at the local park weekly. Now, you play tennis with your friends at the local park every other week.” This is a valid match to gold. The KU 4/4 oracle score should stand.

Updated implication: the remaining Waggle work is broader than one price-comparison case. The real current repair set is five cases across four mechanisms: update-pair extraction, cross-session exhaustive aggregation, numeric comparison, exact quote centering, and temporal ordering.

## Repair Patch Results — Context Assembly

Implemented a general context-assembly repair pass for the five rebuilt-oracle-solvable Waggle misses. The patch is not case-ID based; it adds question-shape evidence lanes and scoring for:

- previous/current frequency questions (`how often`),
- cross-session replaced/fixed item aggregation,
- price-difference comparisons,
- temporal ordering with relative dates,
- exact quote/detail detection.

Focused tests:

- `PYDANTIC_DISABLE_PLUGINS=1 .runtime-build-venv/bin/python -m pytest tests/test_recursive_context_evidence_priority.py tests/test_longmemeval_full_conditions.py`
- Result: `57 passed`

Dry-run artifact:

- `runs/longmemeval/s-waggle-repair5-postpatch-dryrun-20260731-v4`

Paid repair artifacts:

- `runs/longmemeval/s-waggle-repair5-postpatch-paid-20260731-v1`
- `runs/longmemeval/s-waggle-repair2-candidates-paid-20260731-v1`

End-to-end paid checks:

| case_id | pre-patch Waggle | post-patch status | note |
|---|---:|---:|---|
| f685340e | wrong | correct | Added Answer candidates lane for previous weekly vs current every-other-week tennis. |
| gpt4_ab202e7f | wrong | correct | Added Count candidates for replaced/fixed kitchen items across sessions. |
| 1f2b8d4f | wrong | correct | Added price-difference evidence retrieval/scoring; $800 and $50 now both surfaced. |
| gpt4_b4a80587 | wrong | correct | Added temporal Answer candidates for relative event times: prime lens = a month ago; road trip = last week. |
| 58470ed2 | wrong | still wrong | Not context-assembly fixable: exact Borges quote is absent from Waggle transcript records after ingestion. Raw LongMemEval support contains it, but Waggle stores only later assistant turns for that session. |

Implication for spent fresh21 slice:

- Original Waggle: 15/21
- Rebuilt oracle ceiling: 19/21
- Context-assembly repair checks recovered 4/5 clean oracle-solvable Waggle misses.
- If patched behavior is applied to the spent slice, expected diagnostic Waggle score becomes 19/21, matching the rebuilt oracle ceiling except for no additional rows. This should **not** be reported as a fresh benchmark score because these cases are spent and were used to design/validate the repair.

Remaining real issue:

- `58470ed2` is an ingestion/transcript-fidelity bug, not an answer assembly bug. The raw case has the quote, but Waggle's transcript records for `answer_sharegpt_U4oCSfU_7` contain only two turns and omit the quote-bearing assistant content. Fixing this requires preserving full long assistant turns or chunking long assistant answers during ingestion/transcript storage.

## Repair Patch Results — LongMemEval Ingestion Fidelity

Implemented the `58470ed2` fix in the LongMemEval benchmark ingestion path, not in production `observe_conversation`.

Root cause:

- LongMemEval sessions may start with an assistant message.
- The benchmark loader used the legacy `_pair_session_messages(...)` helper, which only emits user→assistant pairs.
- Any leading/orphan assistant message was dropped before Waggle saw it.
- In `58470ed2`, the dropped leading assistant message contained the exact Borges quote: “The Library is a sphere whose exact center is any one of its hexagons and whose circumference is inaccessible.”

Patch:

- `scripts/longmemeval_full/ingestion.py` now walks LongMemEval messages sequentially.
- Normal user→assistant pairs still go through `graph.observe_conversation(...)`, preserving the intended Waggle extraction path.
- Leading assistant turns, trailing user turns, and other unpaired messages are stored as transcript-only records with metadata:
  - `longmemeval_transcript_only: true`
  - `reason: unpaired_message`
- This preserves benchmark evidence without pretending orphan assistant text is a normal live user→assistant conversation pair.

Regression test added:

- `tests/test_longmemeval_full_conditions.py::test_build_case_graph_preserves_leading_assistant_transcript_turn`
- The test verifies that a leading assistant message is stored as turn 0, keeps the document date, contains the Borges quote, and is marked transcript-only.

Focused test result:

- `PYDANTIC_DISABLE_PLUGINS=1 .runtime-build-venv/bin/python -m pytest tests/test_longmemeval_full_conditions.py tests/test_recursive_context_evidence_priority.py`
- Result: `58 passed`

Dry-run validation:

- Probe dataset: `benchmarks/longmemeval/longmemeval_s_borges_ingestion_probe_20260731.json`
- Run artifact: `runs/longmemeval/s-borges-ingestionfix-dryrun-20260731-v1`
- Forced fresh graph cache: `runs/longmemeval/.graph-cache-s-borges-ingestionfix`

Mechanical result:

- `answer_sharegpt_U4oCSfU_7` now has transcript turn 0 as an assistant transcript-only record containing the long Borges essay.
- The `waggle_production_context` dry-run now includes the exact Borges quote in `Answer-bearing evidence`.
- This closes the last known rebuilt-oracle-solvable Waggle miss at the context/evidence availability layer.

Remaining validation:

- A paid reader/judge check for `58470ed2` is still needed once a current Groq key is available. No `GROQ_API_KEY` was present in the local environment when this audit section was written.
