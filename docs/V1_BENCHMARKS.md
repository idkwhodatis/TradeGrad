# V1 immutable daily close-price benchmarks

## Scope and contract

The original V1 plan calls for a benchmark in the immutable RunConfig and durable ResultBundle, with visible units, definitions, date-window querying, charts and original downloads. This implementation supports an optional, explicitly selected, owner-owned published DatasetVersion and listing in native and portable research, and in their separate final-evaluation paths. It is research comparison only, not V2 execution, investment advice or a claim of investability.

`benchmark: null` remains the default. It preserves existing native and portable RunConfig bytes, native `not_configured` bundles, portable bundles without a benchmark key, and their historical config/result/checkpoint/final/package identities. Selecting a benchmark creates a different configuration hash. No selected dataset is inferred from a task, index name or strategy data. A user may deliberately select the strategy dataset, but only via the same explicit version/listing/hash choice.

The selected contract is:

- `definition_version: daily_close_observed_anchor.v1`
- `dataset: {id, sha256}`: exact published immutable DatasetVersion identity and manifest hash
- `listing_id`: exact listing identity inside that version
- `price_field: close`

The API resolves each reference under the authenticated owner, checks the exact manifest hash, and reads only its immutable hash-verified source file. The wizard shows published version, listing, hash and synthetic classification. A missing/stale selection is an error, never an automatic replacement or reset to None.

## Admission semantics

The bounded V1 comparison deliberately rejects cases whose economic meaning cannot be established:

1. Daily intervals only. Both strategy and benchmark datasets must explicitly declare raw/unadjusted split and dividend price bases, a known provider-native mode, and no unknown quality flags or declared evaluation incompatibility. Adjusted, dividend-adjusted, unknown or incompatible semantics are rejected, not relabeled as raw or total return.
2. CN uses CNY, Asia/Shanghai and XSHG; US uses USD, America/New_York and XNYS. Every strategy listing and the benchmark listing must match those declared units, timezone and calendar. The pinned calendar version and exact expected scored-session list must match. This is an intentionally narrow shared-daily-calendar contract, not a currency conversion or cross-calendar comparator.
3. At least two exact, nonprovisional scored sessions are required. Every expected scored close must exist once. No missing-session fill, duplicate collapse, synthetic starting price or benchmark warm-up is permitted. A timezone-bearing source `as_of` is required and retained, but is not a point-in-time or survivorship guarantee.
4. Native strategy timestamps need an explicitly declared profile timezone. An implicit-timezone `source_compat` configuration cannot select this comparison; unchanged `benchmark: null` source compatibility remains supported.
5. Research preflight checks train/yearly-train and validation windows only. Separate final admission checks the exact holdout. Candidate/policy processes receive no benchmark price file. Native evaluator requests continue to carry `benchmark: null` because the trusted worker derives the comparison after evaluation; the run configuration hash still binds the selection.
6. Admission records conservative benchmark source-listing exposure over exact scored dates, separately for research and holdout, with `optimizer_feedback: false`. The optimizer receives its original strategy metrics, not benchmark comparisons or final holdout results.

The dataset `end_date` must cover the end-exclusive window boundary, consistent with existing dataset admission. Exact calendar and source-file session checks, not a date-range overlap alone, decide compatibility.

## Definition, normalization and limitations

Let `E_0` be the first actual account-equity observation on the first scored session, and `P_0` the selected benchmark's actual close on that same session. Both anchors must be positive and finite. Each account observation must have an explicit timezone and map to exactly one expected scored exchange session. Missing, duplicate or extra scored account observations reject publication. Later account equity can be negative; prices must remain positive and finite.

For each scored session `t`:

- `strategy_observed_return = 100 * (E_t / E_0 - 1)`, unit **percent**.
- `benchmark_price_return = 100 * (P_t / P_0 - 1)`, unit **percent**.
- `excess_observed_return = strategy_observed_return - benchmark_price_return`, unit **percentage_points**.

The metrics are the last-session values, tagged with `daily_close_observed_anchor.v1` and their formulas. This is arithmetic excess over the same observed session window, not annualized alpha or a ratio. The first plotted values are zero by observed-value normalization, not by inserting an initial-cash point. Entry-to-first-close strategy gains/costs are outside this close-to-close observed comparison; the original engine total-return metric remains separate and unchanged.

Raw benchmark close-price change is **not total return**. It does not include distributions, corporate-action adjustment, benchmark trading costs or portfolio implementation. Strategy equity includes the engine's recorded costs, so these are **not cost-matched portfolios**. A split may move raw prices discontinuously. The UI states these limitations rather than implying dividend completeness, cost equivalence or annualized performance. Native result trust remains `untrusted_strategy_process`; comparison math cannot upgrade the strategy accounting. Portable trust remains its existing isolated-policy/trusted-engine classification, not runtime certification.

## Durable evidence and reading

The trusted coordinator prepares exact benchmark observations outside the evaluator, derives a new result after sandbox stop, and publishes the comparison with the normal result artifact. It never edits the raw runner file. Native section hashes and portable artifact hashes cover the derived evidence. Full result and final-package downloads retain source references, file hash, source as-of, synthetic classification, currency, timezone, pinned calendar/version, price semantics, exact scored window, actual close/equity observations, anchors, per-session returns/excess and versioned metrics.

Result validation reconstructs all math from actual persisted observations. API result loaders additionally resolve the owner-owned source version, reread hash-verified source bytes and recompute the entire comparison. Missing sources, changed hashes, mismatched selections and forged math fail closed. Historical None results take the original no-benchmark path.

API/chart projections are bounded independently of original downloads. They omit the duplicate input observation array and retain full-window metrics/normalization. Native query date filters select session dates, never renormalize the visible slice and never apply an individual-strategy-listing filter to the portfolio comparison. Long curves use deterministic uniform sampling, explicitly labeled; it is not an extrema-preserving algorithm. Portable projections retain at most 500 points. The UI plots strategy and benchmark returns together in percent, not price beside equity, and shows excess separately in percentage points.

## Validation evidence and remaining gates

Pure checks cover exact math, units/anchors, scale invariance, excess identities, both native and portable publication, raw-runner immutability, old None behavior, source tampering, calendar/coverage/price-semantics rejection, filtering and bounded projections. Existing sealed fixture config/final-hash tests remain applicable. Frontend tests cover immutable selection, persisted drafts/fingerprints, honest None/incomplete result states; existing full frontend tests and typecheck/build are separate checks.

Real acceptance tests are added, not replaced with mocked API success:

- `tests/integration/test_benchmark_admission.py`: migrated PostgreSQL owner/hash/listing admission, idempotency, no-run preflight, immutable selection, exposure separation and rejection cases.
- `tests/system/test_final_pipeline.py`: existing real native research/final pipeline parameterized with an independently published benchmark, durable comparison/query/package checks, no holdout research prices, no native benchmark mount.
- `tests/system/test_portable_final_pipeline.py`: existing real CN event/US portfolio research/final pipeline parameterized with selected/None benchmark, durable result/package checks and no research holdout observations.
- `frontend/tests/e2e/configured-research.spec.ts` `@benchmark`: real upload, selector, preflight, reload, submission, worker result, actual chart SVG, original download/hash, owner isolation, coverage rejection and explicit return to None.
- `frontend/tests/e2e/portable-research.spec.ts`: real CN/US portable flow retains None for CN and explicitly imports/selects a separate benchmark for US; result/final plots and reload retain evidence.

The local implementation environment forbids sockets, PostgreSQL/Docker/browser starts and candidate/provider-SDK execution. Consequently these PostgreSQL/system/Playwright tests are **collected only here, not executed or certified**. Exact-head hosted gates must run them on the permitted real stack, inspect artifacts/screenshots, and fix any observed runtime mismatch before claiming V1 acceptance. Synthetic fixture success, once obtained, is pipeline evidence only, not market-data or benchmark-return validity.

Do not run candidate-executing tests locally, including `tests/tradegrad/test_results.py`, `test_results_entrypoint.py`, `test_window_engine_bridge.py`, `test_result_views_native.py`, `tests/runner/test_research_result_protocol.py` and `tests/portable/test_isolation.py`. An earlier overbroad selection failed during missing pandas/numpy setup before any candidate ran; it is not execution evidence. No dependencies were installed to bypass this boundary.

### Bounded next path

Run the added real PostgreSQL admissions, native and portable final pipelines, and three selected Playwright flows in the existing hosted validation environment. Review native account-date behavior against exact daily session expectations. If the engine does not emit a complete once-per-session equity series, keep selected benchmark runs failed with the explicit alignment error: do not fabricate an anchor or fill gaps. Supporting adjusted or total-return benchmarks requires a separately versioned definition with verified corporate-action/distribution semantics and tests; it is outside this bounded raw-close V1 contract.
