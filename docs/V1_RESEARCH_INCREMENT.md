# V1 strategy understanding and validation increment

This is the separately authorized increment dated 2026-10-23, not V2 or a new
claim that original V1 public-provider certification passed. The original
cumulative deterministic acceptance was GitHub Actions run `39942518018` at
published `42f6cc25f2e4525dcbaad4e58ee95cf733e286cd`. The later live-source gate
failed. SDK diagnostic `39956870702` established Yahoo resolution only, with
AKShare `ConnectionError` and OpenBB `EmptyDataError` (underlying causes unknown).
No live import/snapshot/seed result, paid-model call, broker or deployment is
asserted by this work.

## Reuse and phased acceptance

| Phase | New acceptance boundary | Reused foundation |
| --- | --- | --- |
| I0 | Leaf binding, typed missing states/source anchors, two executable controlled templates and manually derivable frozen fixtures | Evaluation, ExecutionAttempt, immutable candidate code, ResultBundle |
| I1 | Real runtime observations, copied pre-state, immutable partitioned Parquet, bounded owner-scoped reads, trace on/off parity | Native/portable isolated runners, artifact publication and resource fencing |
| I2 | Five-tab workbench, exact source/spec revisions, read-only controlled rule graph and lineage | Existing auth, router, Monaco, tables and charts |
| I3 | Decision/event/stage/instrument deep links, distinct non-trade reasons and execution links | Immutable datasets and result loaders |
| I4 | Frozen two-sided comparison, config differences, source-event pairing, both pre-states and explicit unchanged behavior | Existing config/result comparisons and code diff |
| I5 | Independent diagnostic replay from original start, semantic parity, two/three-group protocols and inherited exposure | Existing queue, lease, cancellation/recovery, experiments/splits |
| I6 | Versioned notes, typed evidence, search, Markdown export, retained/broken references | Owner-scoped persistence, artifact integrity and conservative cold cleanup |
| I7 | Real browser whole-flow acceptance, cross-owner/tamper/legacy/recovery checks and representative performance | Full PostgreSQL/Docker/Playwright/Compose CI |

A phase is recorded as complete only after its own checks. Local pure/unit,
contract and build results do not substitute for the final exact-head hosted
runtime gate. The original branch and its immutable evidence remain intact.

## Frozen I0 semantic choices

- Trace identity is physical `Evaluation.id`, its real `ExecutionAttempt.id`,
  exact result Artifact SHA-256, candidate code hash, dataset/universe versions,
  execution configuration and engine dependency digests. The native runner's
  `attempt_id` parameter is an evaluation sandbox identity, not the DB attempt.
- Optimized candidate code is not the run's seed StrategyVersion code. Both seed
  version and actual candidate/hash are retained; source/spec anchors use actual
  executed code. Old schema-less seed runs have no historical leaf Evaluation;
  no retrospective identity or false/zero/empty observations are invented.
- A cross-sectional decision is one complete portfolio event. Event pairing uses
  common source data event, callback phase, scope and event sequence. UUIDs,
  dates alone and per-stock joins are not event identity.
- Predicate states: true, false, not_evaluated, unknown, not_sampled. Feature
  validity: valid, warmup_insufficient, input_missing, calculation_error,
  not_collected, not_sampled. Absent pre-state is null with a reason, never zero.
- Descriptive StrategySpec is not executed. Controlled IDs map across supported
  parameter changes; arbitrary source mappings remain inferred/unmapped.
- Trace only copies values already computed. Filters affect saved detail, never
  the actual candidate pool. Limits and failures disclose partial coverage.
- Native strategy and engine share a process: observations are not an
  independently trusted ledger/no-lookahead proof. Portable internal logic is
  unavailable unless its actual protocol exposes it. The current portable
  listing cap remains 100; no silent ABI/capacity expansion is intended.
- Diagnostic state reconstruction starts at the source's original start plus
  warmup. Optimizer checkpoints are not complete engine checkpoints. The user
  observation window never replaces the actual reconstruction window.
- Original optimizer zero-closed-trade reward eligibility remains distinct from
  diagnostic/controlled valid non-trading results. No failure becomes zero profit.
- Exposure remains owner-wide and includes family/split lineage. Diagnostics,
  controls, trace reads and note evidence cannot reset final-holdout history.

## Representative measurement protocol, frozen before measurement

The representative **native trace-storage/query/comparison** workload is 1,000
listings × 750 daily sessions, baseline plus two variants, with the full candidate
pool retained in calculations. It does not certify the 100-listing portable ABI.
A separately labelled small real-AKQuant end-to-end fixture verifies runtime
integration. Synthetic trace-shaped data is not called a live market evaluation.

Reference target: Linux x86-64, Python 3.12, 2 dedicated logical CPU equivalents,
7 GiB memory budget, local SSD, PostgreSQL 18.6; record actual CPU, memory,
runtime/package versions and filesystem for every measurement. A different
observed hosted machine is reported, never silently called the target hardware.
The initial indexed single-session candidate page (50 rows, offset bounded) and
single-instrument/stage detail target is p95 ≤ 2,000 ms. At least 30 independent
query samples per shape, both first-open (cold application handle, OS cache not
claimed flushed) and repeated hot reads; report p50/p95/max and sample count.
Report actual rows seen/saved, collection elapsed time, peak RSS, compressed
artifact bytes, first comparison time and cached comparison time. Row/byte
truncation is a failure of the full-scale gate, not a speed optimization.
The first measured baseline will freeze regression budgets; no manufactured
measurement or final SLO is inferred from this initial engineering target.

## Scope restrictions

No arbitrary Python semantic reconstruction, visual strategy programming,
automatic causal proof, model-generated execution facts, vector store, graph
service, broker trading, paid-model calls, merge or deployment. New public data
is never downloaded as a side effect of opening a research page or diagnostic.

## I0 checkpoint

Validated 2026-10-23 before I1 work: 55 tests passed in 14.06 seconds, including both controlled
sources executed by actual pinned AKQuant 0.3.27. Required engine tests compared
trace-on versus trace-off orders, fills, positions, trades, equity and metrics;
future-input changes did not alter earlier observations; narrowed collection did
not shrink the actual strategy universe. Frozen expectations cover B selected,
higher-score C liquidity-filtered, rank losers, retained holdings, warmup,
missed timing, risk and cash constraints, deterministic ties and source anchors.
The observer/contracts also received independent read-only review. Ruff and
`git diff --check` passed. These are local engine/contract checks, not evidence of
PostgreSQL, Docker isolation, HTTP or browser execution for this increment.

Reproduce with an explicitly selected installed native test interpreter:
`TRADEGRAD_NATIVE_TEST_REQUIRED=1 TRADEGRAD_NATIVE_TEST_PYTHON=/path/to/python python -m pytest tests/research_trace -q`.
The required selector fails rather than silently skipping unavailable engine
checks. No real provider, model, broker, deployment or external call was needed.

The first aggregate run found only nonsemantic pandas row-index label differences.
The parity serializer now removes those labels while preserving every row value
and row order; the 55-test aggregate above is the subsequent successful rerun.

## Storage dependency and security boundary

The increment pins DuckDB 1.4.5 LTS rather than the initial 1.4.4 development
candidate. The [official patch release](https://github.com/duckdb/duckdb/releases/tag/v1.4.5)
includes backported out-of-bounds fixes. Existing runtime package versions remain
constrained; only DuckDB is newly added to the API and dependent worker locks.
The [DuckDB security model](https://duckdb.org/security) treats SQL as code and
file structures as trusted input: this application accepts neither user SQL nor
candidate Parquet. Candidate JSON is bounded and validated, the trusted worker
writes Parquet, and reads require immutable manifest/registration/hash agreement.
Automatic extension installation/loading is disabled. This is not a claim that
DuckDB itself supplies a sandbox or that hostile arbitrary files are safe.

## I1 checkpoint

Validated locally on 2026-10-23: **1,920 tests passed** in 63.30 seconds across
research trace, backend, supervisor, operations, immutable window and portable
configuration suites. The engine selector was required and pointed at the pinned
AKQuant interpreter. This includes real native/portable trace off/on/scope parity,
actual native entrypoint and OS-pipe relay checks, genuine partial fills, copied
pre-state, no repeated predicate evaluation, immutable publication and tamper
rejection. Separately, the original economic-result protocol's 43 tests passed
under the native interpreter. Frontend: **501 tests passed**, generated client,
typecheck, production build and format checks passed. Ruff and diff checks passed.
The existing Starlette/httpx deprecation and large lazy vendor-chunk build warnings
remain warnings, not runtime certifications.

Default summary and explicit research/off are staged immutably without changing
historical omitted-field configuration hashes. Four worker paths publish typed
leaf-bound traces in their existing lifecycle/retention guards and fenced result
transaction. Trace transport is separate from the unchanged 8 MiB economic
result envelope: 48 MiB compressed, 16 MiB per decision and the declared bounded
uncompressed capacity. The trusted worker writes Parquet date-range row-group
blocks, not one file per bar. A valid 9 MiB observation survives publication;
7,000 alternating-month events remain complete in fewer than 30 files. Large
individual browser details return an explicit bounded-detail error, while their
immutable artifacts remain available for authorized download.

Owner-scoped APIs and generic downloads enforce the original native/portable
research/final result admission, exact attempt/result/trace pins, source/config
bindings and exposure-before-commit. Candidate filtering includes eligibility,
reason, rank and finite score bounds. Historical absence is null with a reason.
Generic native execution snapshots retain observed order IDs but do not invent
market callback identities; intermediate native order transitions are explicitly
uncollected. Portable internal Python predicates remain unsupported unless its
actual protocol exposes them; its 100-listing ABI cap is unchanged.

Independent boundary review found and then cleared conflict tombstones, missing
rejection reasons, scope enforcement, valid oversized rows, out-of-order date
partitioning and integer/seen-counter limits. The workflow now requires the
capture engine suite and fail-closed PostgreSQL trace selector on the new feature
branch. The 16 new PostgreSQL HTTP cases are **not locally executed**; actual
PostgreSQL, Docker isolation, recovery and browser acceptance remain required at
the final exact-head hosted gate. Local HTTP-stub projection tests are explicitly
labelled and are not substituted for that gate.
