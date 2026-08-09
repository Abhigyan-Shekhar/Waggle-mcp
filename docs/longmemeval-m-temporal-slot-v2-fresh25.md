# LongMemEval-M Temporal Slot v2 Fresh-25 Evaluation

## Status

This is a completed, untouched-slice evaluation. The 25 cases were selected
before the paid run, were not inspected during execution, and must not be
patched and rescored as a fresh result. Any fixes informed by this report need
validation on another untouched slice.

- Dataset: `longmemeval_m_temporal_slot_v2_fresh25_20260809.json`
- Split seed: `20260811`
- Categories: 5 each of SSA, SSU, KU, TR, and MS
- Reader and primary judge: `llama-3.3-70b-versatile`
- Independent cross-judge: `qwen/qwen3.6-27b`
- Embeddings: `all-MiniLM-L6-v2`
- Reader context budget: 3,900 tokens
- Retrieval limit: 10
- Frozen implementation commit: `344968c3`
- Paid artifact: `runs/longmemeval/m-temporal-slot-v2-fresh25-paid-20260809-v1`

No untouched SSP cases remained in the local LongMemEval-M pool after prior
diagnostic work, so this slice makes no SSP claim.

## Primary Results

| Condition | Correct | Accuracy | Average context tokens | Median context tokens |
|---|---:|---:|---:|---:|
| `flat_transcript_vector` | 7/25 | 28% | 2,387.5 | 2,192 |
| `waggle_production_context` | 12/25 | 48% | 3,531.7 | 3,881 |
| `waggle_temporal_slot_context` | 12/25 | 48% | 277.2 | 251 |
| `oracle_answer_turn_context` | 19/25 | 76% | 817.9 | 748 |

Temporal Slot matched the production-context score while using 92.2% fewer
context tokens. It used 88.4% fewer tokens than flat retrieval and 66.1% fewer
than the answer-turn oracle.

Production and Temporal Slot were complementary rather than identical:

- both correct: 8 cases
- production only: 4 cases
- Temporal Slot only: 4 cases
- both wrong: 9 cases

Against the primary oracle labels, Temporal Slot had 11 both-correct cases,
8 oracle-only cases, 1 Temporal-Slot-only case, and 5 both-wrong cases.

## Category Results

| Condition | SSA | SSU | KU | TR | MS |
|---|---:|---:|---:|---:|---:|
| Flat | 1/5 | 2/5 | 2/5 | 1/5 | 1/5 |
| Production | 2/5 | 5/5 | 2/5 | 2/5 | 1/5 |
| Temporal Slot | 3/5 | 4/5 | 3/5 | 1/5 | 1/5 |
| Oracle answer-turn | 3/5 | 4/5 | 4/5 | 3/5 | 5/5 |

At five cases per category, one row changes a category rate by 20 percentage
points. These figures are diagnostic, not stable population estimates.

## Judge Audit

Qwen independently re-judged all 100 answers. It was not used selectively.

- agreement: 91/100
- both correct: 42
- both incorrect: 49
- Llama yes / Qwen no: 8
- Llama no / Qwen yes: 1

Raw Qwen totals were flat 6/25, production 10/25, Temporal Slot 10/25, and
oracle 17/25. These are not automatically substituted for the primary totals:
Qwen made at least two overly strict calls on valid abstentions, while Llama
made at least one objective false-positive on an incorrectly ordered sports
sequence. The nine disagreements therefore require explicit human
adjudication. Until then, report primary and cross-judge totals side by side.

High-confidence judge finding:

- `gpt4_45189cb4`: the oracle gave NFL, college football, NBA, while the gold
  order is NBA, college football, NFL. Llama marked it correct; Qwen correctly
  rejected it.

Likely Qwen false negatives:

- `bc8a6e93_abs`: the gold says no uncle-birthday baking was mentioned; the
  candidate also says no such information was present.
- `09ba9854_abs`: the gold says the bus price is missing; the oracle ultimately
  states that the exact saving cannot be determined.

Rows needing a human policy decision:

- `66f24dbb`: candidates include the gold `yellow dress` plus `earrings`.
- `efc3f7c2` production: the answer includes the correct 30-minute comparison
  but also presents contradictory alternatives.

### Human adjudication of all nine disagreements

| Case | Condition | Llama | Qwen | Human | Reason |
|---|---|---:|---:|---:|---|
| `gpt4_45189cb4` | Oracle | yes | no | no | Objective event order is reversed. |
| `efc3f7c2` | Production | yes | no | yes | It explicitly derives the gold 30-minute answer, although the response is unnecessarily contradictory. |
| `bc8a6e93_abs` | Temporal Slot | yes | no | yes | Correctly says uncle-birthday baking was not mentioned. |
| `bc8a6e93_abs` | Oracle | no | yes | yes | Correctly says uncle-birthday baking was not mentioned. |
| `66f24dbb` | Flat | yes | no | yes | Source says the sister received a yellow dress and matching earrings; the benchmark gold omits the earrings. |
| `66f24dbb` | Production | yes | no | yes | Same source-grounded superset of an incomplete gold answer. |
| `66f24dbb` | Temporal Slot | yes | no | yes | Same source-grounded superset of an incomplete gold answer. |
| `66f24dbb` | Oracle | yes | no | yes | Same source-grounded superset of an incomplete gold answer. |
| `09ba9854_abs` | Oracle | yes | no | yes | It ultimately states that the missing bus fare prevents an exact saving, matching the gold. |

After these adjudications, the aggregate totals remain flat 7/25,
production 12/25, Temporal Slot 12/25, and oracle 19/25. The unchanged totals
hide two offsetting primary-judge errors in oracle (`gpt4_45189cb4` and
`bc8a6e93_abs`), so the adjudication artifact remains necessary.

## Temporal Slot Failure Audit

The 13 primary-judge misses divide into distinct mechanisms.

### Query-routing failures

- `gpt4_7f6b06db`: three-trip chronological ordering was routed as a generic
  current-state query; unrelated travel/running memories filled the slot.
- `gpt4_45189cb4`: January sports ordering was routed as direct fact instead of
  multi-event temporal ordering.
- `efc3f7c2`: Friday-versus-weekday wake-time difference was routed as direct
  fact instead of a two-clock comparison.
- `faba32e5`: a factual duration (`24 hours`) was interpreted as a date-difference
  operation requiring start/end dates.
- `gpt4_65aabe59`: `which ... first, X or Y` was misclassified as set
  enumeration rather than pairwise temporal ordering.
- `dc439ea3`: `which traditional game` was misclassified as enumeration; the
  production pack did retrieve `Hoop Dance`.
- `gpt4_8279ba03`: `10 days ago` was not routed to question-date-relative event
  lookup, so the appliance identity (`smoker`) was never surfaced.

### Required-evidence retrieval gaps

- `0977f2af`: historical-state routing was correct, but the prior `Instant Pot`
  event lost to unrelated cooking/farm memories.
- `27016adc`: percentage routing was correct, but neither `$20,000` renovations
  nor the `$200,000` property price reached its operand slot.
- `8cf51dda`: the full three-objective assistant answer was not retrieved. The
  answer-turn oracle also surfaced only one objective, so this row is not a
  clean Waggle-only repair target.

### Operand and entity-role binding failures

- `e6041065`: the percentage operation used `5-day trip` as both numerator and
  denominator, producing 100% instead of combining 2 worn pairs with 5 packed.
- `09ba9854_abs`: the comparison bound a `$10 train` fare to the requested bus
  operand, calculated `$50`, and failed to abstain despite the missing bus fare.

### Annotation/current-state conflict

- `0f05491a`: all conditions answered 125 stars from an explicit user
  correction, while the benchmark gold is 120. This is an annotation or
  benchmark-state conflict, not evidence that one context condition failed.

## What The Result Establishes

1. Compact typed evidence can preserve production-context accuracy with a
   large token reduction on this slice.
2. The compact and production paths solve different cases; Temporal Slot is
   not yet a replacement for production assembly.
3. The remaining bottleneck is mostly planner/routing and slot binding, not
   context budget.
4. Oracle answer-turn context is useful but not an infallible ceiling, and the
   primary judge is not reliable enough for unreviewed close comparisons.
5. This slice is now spent. It is valid as a frozen evaluation result and as a
   source of future engineering hypotheses, but not as the validation set for
   those hypotheses.

## Next Gate

Do not modify `recursive_context.py` or rescore these 25 cases. The next work is:

1. human-adjudicate the nine judge disagreements and publish a correction file;
2. write failing synthetic/unit tests for the general routing and slot-binding
   mechanisms above without copying benchmark answers;
3. implement one mechanism at a time behind the Temporal Slot path;
4. run the complete focused regression suite;
5. draw a new untouched slice or move to another LongMemEval partition before
   making a new accuracy claim.
