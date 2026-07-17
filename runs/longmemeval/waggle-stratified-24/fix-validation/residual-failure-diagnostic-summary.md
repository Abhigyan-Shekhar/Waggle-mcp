# Residual Known-Failure Diagnostic

Date: 2026-07-17

Scope: diagnostic only. No prompts were changed and no API calls were made.

Artifacts:

- `residual-failure-prompt-context-diagnostic.json`
- `known-failure-validation-waggle-llama70b-postheldout-runtimefix.jsonl`
- `known-failure-validation-0db4c65d-waggle-llama70b-runtimefix.jsonl`

## Summary

The known-failure validation ended at 6 / 8 correct. The two remaining failures are clean and mechanistically different:

- `7401057b`: source-authority conflict, not missing retrieval.
- `a82c026e`: exact compound-answer specificity, not missing retrieval.

These should be treated as named residual failure modes rather than patched and re-scored on the same inspected cases.

## `7401057b`: Hilton Free-Night Count

Question: "How many free night's stays can I redeem at any Hilton property with my accumulated points?"

Gold answer: `Two`

Validation answer: single free night's stay.

Retrieved support IDs:

- `answer_94650bfa_1`
- `answer_94650bfa_2`
- `5e906cb0_1`

Diagnostic result:

- The prompt did include the global conflict/recency rule.
- The prompt did include the knowledge-update rule preferring direct user-stated values over assistant-inferred values.
- The correct evidence was present in context: the user says they have enough points for "two free night's stays" in a later Paris-trip message.
- The wrong evidence was also present multiple times: an assistant states "single free night's stay" in an earlier Hilton/Lake Las Vegas exchange.

Mechanism:

This is not a simple recency-update shape. The correct value is a user-stated fact embedded in an unrelated travel-planning message, while the wrong value is an earlier assistant-inferred/assertive statement. The current rule is framed around "multiple values for the same fact" and "most recent" values. The reader appears to latch onto the earlier assistant answer as the directly relevant Hilton answer and does not treat the later user aside as overriding evidence.

Better description:

`7401057b` is a source-authority and evidence-selection failure: direct user-stated quantity should outrank assistant-inferred quantity even when the user-stated fact appears as an aside in a different topical session.

Do not describe recency as solved. It is partially validated only.

## `a82c026e`: Dark Souls 3 DLC

Question: "What game did I finally beat last weekend?"

Gold answer: `Dark Souls 3 DLC`

Validation answer: `Dark Souls 3`

Retrieved support IDs:

- `answer_787e6a6d`
- `752392bb_3`
- `sharegpt_MBVSMQO_45`
- `sharegpt_CMeL2P1_0`
- `ccaabf0b_1`

Diagnostic result:

- The exact phrase `Dark Souls 3 DLC` appears in the context multiple times.
- The prompt includes the single-session instruction: "If a candidate source hit directly contains the requested fact, answer with that fact concisely."
- The model found the correct game family but dropped the qualifier `DLC`.

Mechanism:

This is an exact compound-noun specificity failure. The model compressed the answer under a concision instruction and omitted a qualifier that the judge/gold answer treats as required.

Better description:

`a82c026e` is an exact-answer fidelity failure: when the source contains a compound answer phrase, exactness should outrank brevity.

## Implication

These two cases should remain acknowledged residual failures in the known-failure validation notes. A future general fix can target:

- stronger source-authority handling for user-stated facts versus assistant-inferred facts;
- exact compound-answer preservation for short factual answers.

But those fixes should be validated on fresh data, not by chasing an 8 / 8 result on this inspected set.
