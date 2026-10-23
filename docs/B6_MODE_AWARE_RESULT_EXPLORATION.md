# B6 mode-aware result exploration follow-on

## Accepted cumulative fixture evidence, 2026-10-23

The complete fixture gate passed in [run 39942518018](https://github.com/idkwhodatis/TradeGrad/actions/runs/39942518018)
at head `42f6cc25f2e4525dcbaad4e58ee95cf733e286cd`, tree `a99216d3fe10b5c5015627e78cabad1e5de66577`.
See [PC and exact suite counts](V1_IMPLEMENTATION.md#original-v1-cumulative-fixture-evidence-accepted-pc).
This evidence is for that exact source, not live upstreams or production deployment.

Actual native/portable research and final exploration, native recorded indicators,
immutable benchmarks, owner-scoped bounded query projections and persisted browser
reload/navigation passed in PC. The dedicated indicator/exploration group passed
its one case; other mode checks belong to their application/browser groups.
Portable indicators are not invented, policy output is not trusted accounting,
and chart filtering does not recompute optimizer metrics or certify session coverage.

This source change closes the native-final and portable research/final explorer,
and named listing/candidate query gaps recorded in the earlier native-only B6
slice. Its cumulative real runtime fixture evidence is now PC. The original
implementation/validation history in the acceptance matrix is not rewritten
by this later result.

## Evidence and trust boundaries

- Each projection carries an immutable exploration context: actual run mode,
  evaluation/run IDs, original artifact SHA-256, original dataset ID/SHA-256,
  split/window/timezone, result trust, ledger origin, pinned listing mapping, and
  final selection ID when applicable. Portable staged-market-data SHA-256 is a
  separate field, never substituted for the original dataset identity.
- Native final projections use the existing final result admission, frozen
  selection binding, and holdout exposure path. Portable final projections use
  their own final admission/exposure path. Research routes cannot expose holdout
  results. Owned immutable artifact loaders remain authoritative.
- The dataset manifest checksum is verified before projection metadata or files
  are read. OHLCV comes from checksum-verified immutable dataset files; execution
  prices never become candles. Missing price sessions are not synthesized.
- Native observations retain `native_strategy_and_engine_share_process` /
  `native_same_process`. Portable accounting uses only admitted
  `isolated_policy_trusted_akquant_engine` / `trusted_engine` account observations
  and fills, never policy state or policy-reported PnL.
- Portable markers retain original native timestamps, nanoseconds, fill/order
  IDs and economic next-open references as distinct values. Chronological
  navigation retains original fill-row identity, including after table sorting.
- Portable indicator observations are explicitly unavailable: the current
  trusted-engine result protocol has no declared, persisted indicator section.
  Neither strategy names nor policy state are used to invent indicator values.
  Native final views reuse only indicators actually persisted by the native
  recorder; absent/legacy/unsupported series retain visible unavailable states.
- No changes to original metrics, optimizer rewards, benchmark normalization,
  result bytes, native trust labels, or final selection feedback are introduced.
  Frontend adapters reject mismatched mode/artifact/window/final-selection/trust
  contexts instead of coercing portable artifacts into native-shaped results.

## Surfaces and bounds

All four result modes have OHLCV/fill replay, selected-execution details,
previous/next execution navigation, monthly returns and return distribution.
Native research keeps its original B6 implementation; the specialized native
final and two portable pages now share the mode-aware visuals.

The return analysis definition remains `tradegrad.observed-equity-returns.v1`:
engine-local final daily equity observations, observed month-end/baseline equity
ratios and adjacent observed-equity returns, with the existing invalid-base and
pinned-calendar gap exclusions. No initial cash or missing close is fabricated.
Partial periods, missing sessions, unverified calendar coverage, exact bin
boundaries, and the full persisted portfolio scope remain visible. Table/date
filters do not silently recompute stored full-window metrics or this analysis.

New mode routes (under `/api/v1`):

- `/final-evaluations/evaluations/{id}/return-analysis` and `/price-replay`
- `/portable-evaluations/{id}/return-analysis` and `/price-replay`
- `/portable-final-evaluations/evaluations/{id}/return-analysis` and `/price-replay`

Existing result-table routes support allowlisted server-side sorts and side,
date and listing filters where those fields exist. Portable chronological
account/decision series cannot be arbitrarily reordered. Sorting/filtering
precedes pagination; original row indices are returned. Unsupported combinations
are rejected rather than silently ignored. Table limits remain 500 server-side
and 50 in the portable/final UI. Native large returned series reuse the existing
virtualized table. Replay remains bounded to 500 bars server-side, 250 in the UI,
2,000 markers, and the existing file/row budgets. Navigation is within the
returned replay page; reviewing a table fill opens a bounded date neighborhood.

The universe securities/listing chooser now has literal symbol search,
MIC/currency filters and symbol/MIC/currency/creation sorts. Native and portable
candidate lists have literal key search, kind/minimum-iteration filters and
allowlisted identity/generation/iteration/creation sorts. Queries are owned,
parameter-bound SQL expressions with literal wildcard escaping, filtered counts,
stable ID ties, at most 100 rows per page and a 100,000 offset bound. Existing
selections stay bound to IDs across queries; these controls do not re-rank or
select a candidate automatically.

## Validation performed locally

All executions below are pure or build-time checks, not engine execution:

- 259 Python tests passed across result exploration, mode projections, research
  and final views, and portable immutable-admission/export/HTTP projections.
  One existing Starlette/httpx deprecation warning was reported.
- 447 frontend unit/render/transport tests passed across 30 files. In-memory
  transport tests are labeled unit tests and are not browser/integration proof.
- TypeScript checking and the production Vite build passed. Vite reported the
  existing large lazy-loaded chart/editor chunk warning.
- Changed authored Python/TypeScript sources passed Ruff/Prettier checks and
  `git diff --check`.
- OpenAPI and TypeScript client generation succeeded locally. Generated files
  are deliberately excluded from the source patch and must be regenerated after
  integration with the other completed slices.

Collected only, **not executed**:

- 23 Python PG/system cases: 14 real PG cases (6 new named-list cases and 8
  existing native exploration cases), and 9 real system pipeline cases. The
  system cases cover the previous native research pipeline plus native final
  (2), portable research (2), and portable final (4) flows. Assertions consume
  the actual verified result after execution, compare marker fields with the
  original ledger, test foreign-owner rejection, and re-download unchanged
  original bytes. No fixture admission substitutes for those gates.
- Four changed top-level Playwright scenarios: native recorded-indicator replay,
  native frozen final, real portable CN/US research plus final workflows, and
  universe selection/query persistence. The portable scenario covers both
  portable explorers. Shared helpers check actual HTTP projections and rendered
  charts, fill selection, unavailable indicators, sort queries, foreign-owner
  isolation and reload. No route mocks replace these flows.

No PG/socket/Docker/browser startup, local strategy/SDK subprocess execution,
paid call, live trade, commit, publication, workflow change or root README edit
was performed. The explicitly excluded engine-dependent tests were not run.
The exact integrated-head PG, native/portable system pipelines, and Playwright
execution remain required acceptance gates in a permitted environment.

## Integration notes

Baseline: `e98a42b6717108acba492e3bad0d11d498471e68`.

The later B9 source slice adds immutable `data_evidence` and declaration notices.
Preserve those fields, `portable_data_evidence`, and notices. Combine the trailing
`OwnedPortableResult` fields and use keyword arguments for both `evidence` and
`dataset`; neither replaces the other. Do not remove B9's final-result evidence
projection or operator-model labels.

The system test changes here are limited to shared exploration assertions and
imports/calls. Preserve the parent's separate process-ownership/lifecycle
hardening. Regenerate the contract/client after combining source schemas, using
the repository generator without formatting generated client output. Historical
acceptance-matrix consolidation is intentionally left to the parent integration.

## Renderer accessibility integration check, 2026-10-23

Static inspection of pinned ECharts 6.1.0 showed that enabling automatic ARIA
generation replaces the chart root's JSX label. Monthly and distribution charts
now pass the same explicit description to the renderer and DOM, preserving the
partial-period and non-certified-daily-return caveats. Two component-contract
regressions check both descriptions and series types; the existing real-browser
scenario additionally requires visible SVGs under those named charts.

The full frontend unit suite passed (492 cases), as did typecheck, production
build and changed-file formatting. These component checks do not execute a real
browser. The cumulative hosted chart scenario subsequently passed at PC;
this repair does not change the earlier M5, M6 or native-operator stage trees.
