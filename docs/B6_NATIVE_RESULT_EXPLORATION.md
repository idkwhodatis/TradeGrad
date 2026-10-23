# B6 native research result exploration slice

Implemented against `071bad02a3928f29760529bbb0089f68df8e2ab7`. Validation date:
2026-10-23 UTC. This is a bounded implementation of original-plan V37–V39,
not a claim that generic V1 result exploration is complete.

## Delivered behavior and evidence boundaries

- Native research Backtest Detail (training and validation evaluations) displays
  the evaluation's checksum-verified, immutable dataset OHLCV, actual persisted
  fill markers, selected execution details, and previous/next navigation within
  the returned price page. Selecting a fill in the server-filtered table preserves
  its original artifact row index, switches to its pinned listing, and focuses a
  bounded date window. It does not identify fills by the sorted display index.
- `/research-evaluations/{id}/price-replay` uses the existing owner-authorized,
  checksum-validated result loader and pinned evaluator symbol mapping. Dates are
  engine-local dates, not browser-local dates. Missing-price executions remain in
  the execution list as unavailable markers; they are never snapped to another
  date or rendered using their fill price as an OHLCV candle.
- Actual indicator definitions, instances and observations are copied from the
  live AKQuant result into an optional hashed `indicators` artifact section. The
  locked AKQuant 0.3.27 source was inspected read-only. No engine was run locally.
  Only explicitly recorded observations are exported. There is no source-code
  import, AST inference, technical-indicator reconstruction or fabricated series
  in the API or worker. New artifacts without recordings say `not_recorded`;
  legacy artifacts without the section say `not_persisted` in the view.
- `/research-evaluations/{id}/return-analysis` derives display-only monthly and
  distribution summaries from the full persisted portfolio equity, independently
  of table filters and chart downsampling. Heatmap cells, histogram bins and
  bounded accessible numeric tables include definitions and unavailable states.
- Positions, orders, fills and closed-trade tables support explicit server-side
  sort allowlists, date/listing filters, order/fill side filters and order-status
  filters where applicable. Unsupported fields/combinations fail closed. Sorting
  and filtering precede pagination, ties are stable and nulls sort last in both
  directions. Chronological equity/drawdown series cannot be arbitrarily sorted.
  Existing result-table virtualization remains, with transaction pages of 50.
- Native strategy/engine same-process trust labels are unchanged. These views
  are not independent accounting or evidence of correct strategy behavior.
  Original metrics, optimizer rewards, raw artifacts, benchmark basis and
  benchmark normalization are unchanged. The native benchmark rehash helper
  includes the optional indicator section rather than dropping its binding.

## Indicator authoring and presentation contract

The supported producer is AKQuant's existing explicit recorder, for example in
the evaluator-side strategy callback (this is documentation, not API execution):

```python
self.record_indicator(
    "observed_close", float(bar.close), symbol=bar.symbol,
    display_name="Observed close", unit="currency_per_share", pane=0,
)
```

The recorder's declared metadata and actual returned values are persisted under
`akquant.0.3.27.recorded-indicators.v1`. The contract validates definition and
instance references, symbol/owner/key agreement, ns/ms timestamp agreement,
per-instance ordering and finite numeric values. Nonfinite missing recordings
become JSON null, never interpolated values. Warm-up points remain in the raw
artifact but are excluded from evaluation-window charts. Multiple observations
on one engine-local date collapse to the last recording, with an explicit count.

The V1 chart renders declared line and histogram series with pane/unit labels.
Other render types remain visibly unsupported with raw metadata/values available.
Metadata such as reference lines, colors, precision and scale-group identifiers
is retained, not a promise that every AKQuant renderer option is implemented.
The frontend uses safe display colors rather than interpreting arbitrary style
or executable content. Indicator absence is not repaired by computing a default
moving average or by inferring an indicator from a strategy name.

Bounds: 64 definitions, 256 instances, 100,000 observations and 2,000,000 serialized
bytes; panes 0–16. Exceeding the supported contract produces an explicit
`unsupported` indicator section with an explanation and no substitute values.
The overall result artifact remains subject to its pre-existing size budget.

## Return definitions and missing-data policy

1. Take the last persisted equity observation per engine-local calendar date.
   Preserve full numeric precision; report collapsed intraday observations.
2. Monthly return is the month's last observed equity divided by the preceding
   calendar month's last observed equity, minus one. If no preceding-month close
   exists, use the first observed close as a **partial-period** baseline. Never
   substitute initial capital. Window-clipped months are also partial.
3. Empty months, a baseline skipping an entire calendar month, fewer than two
   closes, missing/nonfinite values, or nonpositive denominators are unavailable.
   No zero-return cell is invented. A negative terminal equity is not itself a
   missing observation, but cannot serve as the next positive denominator.
4. If every pinned evaluator listing has versioned `coverage.expected_sessions`,
   use the union of those persisted dates in the evaluation interval. Missing or
   unexpected observations are visible. Affected monthly returns are unavailable;
   distribution intervals crossing missing sessions are excluded. Do not obtain
   a newer exchange calendar or silently regenerate the historical index.
5. Without that complete pinned coverage, completeness is explicitly unverified.
   Distribution returns are then **inter-observation**, not certified one-trading-
   day returns. No sessions are filled and no completeness claim is made.
6. Distribution values are unannualized `E[t] / E[t-1] - 1`, subject to the above
   rules. Equal-width bin count is `ceil(sqrt(N))`, capped at 20. Bins are
   `[lower, upper)` except the last includes its upper boundary; identical values
   form one bin. Invalid intervals are counted separately.

These are view definitions, not new objective metrics. No added fees, benchmark
values, cross-listing allocations, annualization or strategy-reward changes are
introduced. The monthly output is bounded to 1,200 months.

## Resource and navigation bounds

Price replay returns at most 500 bars per request (UI pages: 250). It scans only
the pinned listing and requested evaluation dates, with at most 1,200 references,
40,000 rows and 16,000,000 bytes of verified price chunks. Oversized requests are
explicitly unavailable rather than silently sampled into misleading candles.
At most 2,000 executions are returned per page with explicit truncation status;
the UI pages the execution list by 50. Missing-bar executions are partitioned
between price-page boundaries so that they are not silently discarded. Navigation
is page-local; it is not a claim that the browser holds every fill in a large run.
The original persisted result remains downloadable unchanged.

## Exact original-plan coverage gaps and bounded continuation

The supplied plan's V37–V39 covers result exploration more broadly than this
native research page. The following are **unimplemented product gaps**, not
merely external-test prerequisites and not an alternative definition of done:

- Native final-test detail has not received the new replay, recorded-indicator,
  selected-execution, monthly or distribution panels/endpoints. Its only change
  here is compatibility with the shared response's explicit engine timezone.
- Portable research and portable final-test detail have not received these new
  interactions/analysis panels. Their existing trusted-ledger provenance is not
  converted to native result provenance to reuse a component.
- Interactive server sorting/filtering of the plan's named securities and
  candidate lists is not added here. New allowlisted sorting covers result
  transaction/position tables only; existing fixed list ordering is not counted
  as user-selectable server sorting.
- Arbitrary strategies that did not explicitly record indicators still cannot
  show actual indicator histories. Unsupported recorder metadata/render types
  remain visible limitations, not decorative placeholder charts.

A finite follow-on can reuse the pure projections without weakening boundaries:

1. Add final-test adapters/endpoints using the existing final-result owner,
   terminal-state and exposure/access-audit checks before invoking projections.
   Do not bypass holdout/exposure handling by routing final IDs to research APIs.
2. Define portable adapters from its independently verified ledger and pinned
   price evidence. Preserve portable execution/benchmark identities and trust
   labels. Keep indicators unavailable until an explicit persisted declaration
   and observation protocol exists; never synthesize native sections for them.
3. Make the frontend panels accept mode-specific typed query adapters, then wire
   native final and both portable pages with exact-mode auth, reload, missing-data
   and linkage tests. Do not display charts ahead of their validated evidence.
4. Add bounded allowlisted securities/candidate query contracts and interactive
   controls, testing filters/sorts before pagination and stable identity.

Parent integration must combine adjacent B5 `native_contract.py` and B6
`indicators.py` environment-input/Docker COPY additions. Regenerate OpenAPI/client
after combining parallel API patches; generated files here describe this isolated
slice only. No Settings, worker, data-runtime or Compose changes are included.

## Validation performed, and gates not run

Locally executed without services, browser starts or strategy/SDK execution:

```sh
python -m pytest backend/tests/test_result_exploration.py \
  backend/tests/test_research_views.py backend/tests/test_final_result_views.py \
  backend/tests/test_contracts_storage.py \
  tests/tradegrad/test_benchmark_comparisons.py -q
# 216 passed; one existing Starlette/httpx deprecation warning.
python -m backend.app.export_openapi
cd frontend && npm run generate-client && npm test && npm run build
# 426 tests passed in 26 files; tsc and production build passed.
# Build retains large lazy ECharts/Monaco chunk warnings, not a browser result.
```

Ruff, formatting and patch-whitespace checks also pass. Added acceptance gates
use actual boundaries, not mocked replacement services. They were **collected
only**, never counted as passes:

- `tests/integration/test_result_exploration_http.py`: 8 real migrated-PostgreSQL
  tests with authenticated ASGI requests, immutable artifact/chunk bytes, owner
  isolation, pre-pagination filtering, unchanged download and corruption refusal.
- `tests/system/test_result_exploration_pipeline.py`: 1 real HTTP/PG/queue/data-
  worker/Docker evaluator test. It persists recorder strategy source through the
  API and checks actual exported observations/fills after worker completion.
- `frontend/tests/e2e/configured-research.spec.ts`, grep
  `persisted execution replay`: 1 Chromium flow, real API/PG/workers/Docker, no
  fabricated response data. It exercises wizard submission, recorded charts,
  fill navigation, server sort/filter, raw-download identity, reload and owner
  isolation. Synthetic prices and frozen models are explicitly not live evidence.

To execute later, provision the existing test infrastructure in an authorized
environment. PG integration needs disposable `TEST_DATABASE_URL`. The system
gate additionally requires `TRADEGRAD_REQUIRE_RESEARCH_INTEGRATION=1`, separate
`API_PYTHON`, `RESEARCH_PYTHON`, `DATA_PYTHON` runtimes and the exact-input evaluator
image. Browser execution needs the existing E2E accounts/configuration, live
local test services and installed Chromium. Run the commands below only there:

```sh
python -m pytest tests/integration/test_result_exploration_http.py -q
python -m pytest tests/system/test_result_exploration_pipeline.py -q
cd frontend && npm run test:e2e -- --grep 'persisted execution replay'
```

No sockets, PostgreSQL, Docker, browser, new/changed strategy or SDK subprocess
was started for this slice. Engine-dependent result/entrypoint/window tests,
runner result protocol and portable isolation were excluded locally, including
`tests/tradegrad/test_result_views_native.py` whose optional-section hash fixture
was updated but not executed. All those exact-head gates, plus visual/browser
review and broader existing regression gates, remain unrun release requirements.
No commit, publication, deployment, paid call or live trade was performed.

## Integrated preparation checkpoint

Integrated after the Settings, benchmark, provider, strategy-authoring and
operations slices on 2026-10-23 UTC. The combined Docker image includes both
native declarations and the recorded-indicator contract. The workflow explicitly
requires the real recorder pipeline and sanitizes its outcome-only JUnit report.
Local combined validation: 216 pure Python tests and 432 frontend tests passed;
TypeScript, production build, formatting, targeted Ruff, whitespace checks and
46 workflow shell-block syntax checks passed. Nine added PostgreSQL/system cases
were collected only, not executed. Generated clients are the untouched output of
the frozen generator, matching CI's regeneration check. Hosted engine/browser
acceptance and the broader follow-on listed above remain pending.
