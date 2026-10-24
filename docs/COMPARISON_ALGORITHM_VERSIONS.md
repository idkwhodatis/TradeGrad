# Observed-comparison identity and compatibility

This is a correctness repair within the V1 research increment, not WebUI V2.

## Immutable identities

New comparisons use `tradegrad.observed-comparison.v2`. The version is included
in both the engine input hash and the owner-bound request snapshot/cache key.
Status responses and result manifests declare it explicitly; the UI checks
that they agree. Unsupported versions and mismatched pinned hashes fail closed.

`tradegrad.observed-comparison.v1` remains readable at its original input,
manifest and artifact hashes. Saved rows and conclusions are not rewritten.
This includes legacy and mixed physical Parquet row representations; logical
algorithm version is separate from the lossless row codec version. No migration
rewrites comparison or note history. Historical UI results carry a warning that
the old algorithm could mistake uncollected strategy observations for absence.

Resubmitting an original idempotency key retains that submission's exact saved
algorithm and limits. A new key requests current semantics and cannot hit the
v1 cache. Retrying or executing queued v1 computation is rejected with
`comparison_algorithm_unsupported`, before source querying or computation.
Historical finished results remain read-only. Submitting a new comparison is
explicit and never retargets old notes, evidence pins or links.

## Corrected absence semantics

Retaining all emitted decision records does not establish that a source emitted
all strategy observations. Execution-only native completion snapshots and mixed
generic/source-emitted traces cannot certify collection of strategy candidates,
features, predicates or targets. Missing observation capability stays unknown
or not collected, even when economic orders/fills are fully available.

Capabilities are derived from recorded source callbacks, exact phase/scope/
instrument and recorded collection information, not from nearby timestamps or
date/symbol joins. They do not change event pairing. Explicitly linked native
completion events remain paired; genuine one-sided engine events retain their
own identity and missing pre-state. Complete supported source absence remains
distinguishable from missing observations. Explicit null and empty collections
are not interchangeable.

Stage capability requires presence across all retained callbacks of that exact
kind, not a union of stages emitted on some callbacks. Raw and constrained
target collection is tracked separately; an intermittently null target cannot
certify absence. Summary-mode detail, filtered or dropped stages, and named
instruments outside the capture scope remain uncollected or incomplete. Global
feature/predicate observations remain available under an instrument filter.
Explicitly retained rows always remain observations.

Capability allocation shares the existing event-index envelope and aborts on
capacity overflow. Target-presence checks use a bounded aggregate only when a
genuinely unmatched event could otherwise claim absence; fully paired traces
perform no added capability index or stage scan. This repair does not increase
the output, page, record,
DuckDB memory, row, thread, spill or deadline bounds. The frozen capacity data,
protocol and original failed/successful baseline reports remain unchanged.
Every final candidate still requires the complete hosted 1,000-instrument ×
750-session × three-version capacity gate, including exhaustive round-trip and
source/query coverage audits, plus PostgreSQL, Compose/browser and recovery.
