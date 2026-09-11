# Recovery delivery successor

The sealed V2.2 full-500 result is 304/500. Inspection of its packets found
232 recovery cases without an appended legacy operation bundle, no explicit
reference-date framing in any prompt, and one computed operation rejected by
its serialization budget. The reported 22 structured activations counted
computed results rather than exclusively delivered results.

This successor adds an opt-in recovery delivery module. Callers supply the
existing core, query, original source objects, query reference date and exact
token counter. It ranks exact user paragraphs, gives distinct sources one
admission each before additional passages, excludes future sources, and keeps
character/byte offsets and SHA-256 provenance. The baseline is preserved.
The default added budget is 1,600 tokens, at most eight passages. This is a
larger-context intervention, not an equal-budget causal comparison.

Collection completeness remains UNKNOWN. Source diversity does not certify
an exhaustive inventory. Abstention remains appropriate when evidence is
missing; the intervention improves the evidence presented before the reader
decides whether to abstain.

A separate conservative operation helper supports uniquely bound event dates
with query-selected units and explicit named-entity currency sums. It rejects
ambiguous operands, future dates, uncertain statements, and unsupported
collection closure. Calendar months require matching day-of-month; it never
silently converts an arbitrary day difference into months. This helper has
not established improved benchmark operation coverage.

Nine unit tests cover exact Unicode provenance, source diversity, reproducible
selection, future exclusion, budget skips, deduplication, conflicting source
IDs, arithmetic units, ambiguous operands and absent closure.

The first exposed development smoke uses nine failures and three controls.
Its packets preserve 12/12 baseline prompts, append 70 passages, and have at
most 1,599 extra tokens. No new structured result activates on this sample.
The reader remains Codex Luna low. All raw outputs must be sealed before
blind semantic judgment. Do not promote based on tests or passage counts.
Smoke gate: at least one failed case repaired and no control regression.

Full-500 rerun remains pending the smoke result. The existing V2.2 artifacts
are preserved. Any subsequent 500-case result is development-exposed.
