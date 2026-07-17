# Judge Bias Audit

## Scope

This audit combines the Qwen adjudications completed so far:

- LongMemEval-S disagreement rows:
  `runs/longmemeval/waggle-stratified-24/fix-validation/second-judge-disagreements-qwen3-32b-final.jsonl`
- Targeted stress key rows:
  `runs/longmemeval/targeted-stress/stress-v1-second-judge-qwen3-32b-key-rows-normalized.jsonl`

Total inspected rows: `30`.

## Flip Direction

| source | rows | Llama no -> Qwen yes | Llama yes -> Qwen no | agree yes | agree no |
|---|---:|---:|---:|---:|---:|
| LongMemEval-S | 24 | 3 | 1 | 11 | 9 |
| Targeted stress | 6 | 4 | 0 | 2 | 0 |
| Combined | 30 | 7 | 1 | 13 | 9 |

## Interpretation

The inspected rows skew toward Llama false negatives: seven rows moved from
Llama `no` to Qwen `yes`, while only one row moved from Llama `yes` to Qwen
`no`.

This does not prove a global judge-bias rate, because the rows were selected
from disagreements and suspicious failure cases rather than sampled uniformly.
It does justify treating Llama-only scores as conservative until a second judge
checks the rows most likely to affect the conclusion.

## Important Distinction

The targeted stress suite did not replicate the real `7401057b` source-authority
failure after Qwen rejudging. That does not invalidate `7401057b`; it means the
synthetic source-authority cases were not isomorphic to the real failure.

The real case had a much larger assembled context, older single-night evidence
appearing early, and the correct two-night user evidence appearing mid-context.
The synthetic cases were short, direct, and easy for both retrieval paths and
the reader.
