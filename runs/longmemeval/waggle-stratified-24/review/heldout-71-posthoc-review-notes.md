# Heldout 71 Post-Hoc Review Notes

Status: analysis only. The heldout 71 outputs are spent and should not be used for prompt/retrieval tuning or re-scored after fixes.

## Raw Result
- Raw Llama-judge score: flat_vector 55/71, waggle_full 51/71.
- This is not evidence of a Waggle QA win on the blind slice.
- Reader and judge were both `llama-3.3-70b-versatile`, so disagreement cases need second-model adjudication before a paper number is finalized.

## Judge Audit
- Prepared second-judge payload: `second-judge-disagreement-payload.jsonl` with 24 judgments from the 12 flat-vs-Waggle disagreement cases.
- Clear judge false positive found manually: `80ec1f4f`, gold `2`, Waggle answered effectively `1`, original judge marked yes.
- Likely judge false negative found manually: `bf659f65`, gold `3`, flat listed exactly three albums/EPs, original judge marked no.
- No OpenAI/Anthropic API key was available locally, so GPT-4o/Claude adjudication was not run yet.

## Context Position Diagnostic
Six Waggle answer-assembly failures were reconstructed through the harness context path without reader/judge calls: `73d42213`, `7401057b`, `8752c811`, `a82c026e`, `dfde3500`, `f523d9fe`.

| case | category | context tokens | evidence position finding | mechanism read |
|---|---:|---:|---|---|
| `73d42213` | MS | 6810 | answer_1881e7db_1 first@0.271; answer_1881e7db_2 first@0.001; terms two hours@0.01, 7 AM@0.282 | Not pure lost-in-middle: travel duration appears near start; computed answer 9:00 is absent and must be inferred. |
| `7401057b` | KU | 7025 | answer_94650bfa_1 first@0.001; answer_94650bfa_2 first@0.264; terms two@0.152, single free@0.109, Hilton@0.004 | Signal conflict/noise: older single-stay evidence appears early; two-stay evidence appears mid-context. |
| `8752c811` | SSA | 3838 | answer_sharegpt_6pWK9yx_0 first@0.002 | Evidence truncation/assembly loss: exact gold term not present in reconstructed Waggle context. |
| `a82c026e` | SSU | 5116 | answer_787e6a6d first@0.114; terms Dark Souls 3 DLC@0.154, last boss@0.152, Dark Souls 3@0.12 | Exact answer present near front; reader dropped qualifier `DLC`, likely answer specificity issue. |
| `dfde3500` | KU | 5396 | answer_35d6c0be_1 first@0.258; answer_35d6c0be_2 first@0.001; terms Juan@0.268 | Gold weekday absent as exact term; support present but needed detail likely truncated or not surfaced. |
| `f523d9fe` | SSA | 4847 | answer_sharegpt_m2xJfjo_0 first@0.079 | Evidence truncation/assembly loss: exact gold term `Doc Martin` absent from reconstructed Waggle context. |

## Mechanism Summary
- Lost-in-the-middle is plausible in some cases, but it is not the dominant explanation for the named failures. Several failures had support or answer-relevant terms near the first 10-30% of the context.
- There are at least three mechanisms: evidence truncation/absence (`8752c811`, `f523d9fe`, partly `dfde3500`), signal conflict from older structured/source evidence (`7401057b`), and answer specificity/computation failure despite evidence being present (`a82c026e`, `73d42213`).
- Waggle contexts were much larger than flat contexts on average: about 5,578 vs 2,015 context tokens. The issue is better described as context dilution plus ordering/truncation, not simply retrieval failure.
- Any fix should be designed on a new slice, not these 71 cases.

## Files
- `heldout-71-case-review.csv`: per-case answers, scores, support coverage, tokens, files.
- `heldout-71-case-matrix.md`: compact one-row-per-case matrix.
- `waggle-context-position-diagnostics.json`: support ID positions in reconstructed Waggle contexts for six failures.
- `waggle-answer-term-position-diagnostics.json`: answer-term positions for the same six failures.
- `second-judge-disagreement-payload.jsonl`: second-judge input payload for GPT-4o/Claude.