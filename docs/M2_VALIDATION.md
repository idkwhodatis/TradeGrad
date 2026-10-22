# M2 data workbench: acceptance status and limitations

## Status

**Prepared locally, not yet accepted.** The new M2 PostgreSQL, real-process,
Docker, Chromium and live-provider gates have not run against this code revision.
Earlier M0/M1 hosted results remain evidence for their recorded exact commit,
not for this M2 revision. See [M1 evidence](M1_VALIDATION.md).

No paid model requests, live trading, production deployment or merge is part of
this stage. This is not a completed V1 research product.

Local checks on 2026-10-22 passed **254 tests**: 32 actual four-task baseline,
81 API/supervisor/storage/runtime checks, 98 offline data/SDK contracts, and 43
frontend unit checks. All three live probes were deliberately excluded from that
run, not counted as passes. The API and data runtimes matched all 49 and 105 locked
package versions respectively. OpenAPI/client regeneration produced identical
bytes; formatting, types, production build, Ruff and whitespace checks passed.
Collection found 187 real-PostgreSQL tests plus two actual-process recovery tests,
and Playwright discovered 20 Chromium scenarios, three of them live-provider
eligible. Collection/discovery is not execution. The Docker suite is likewise
pending on the M2 revision.

## Implemented data boundary

- Three independent AKShare, yfinance and OpenBB/Nasdaq adapters, with explicit
  server-selected fixture or live transport and no automatic provider fallback.
- Authenticated resolve/import/preview/coverage/refresh APIs, durable PostgreSQL
  imports, Procrastinate dispatch, shared endpoint budgets, generation fencing,
  cancellation and retry using completed content-addressed checkpoints.
- Immutable raw SDK returns, normalized Parquet, coverage/quality reports,
  provenance, prices/actions, listing mappings, universes and manifest hashes.
  Raw SDK tables preserve their exact Arrow representation; this is not a claim
  to capture original HTTP wire bytes.
- Owner-scoped CSV/Parquet uploads with explicit mappings, bounded parsing,
  preserved unknown fields, one exact original source and no repeated full-file
  expansion across date blocks. Unknown execution units block seed evaluation.
- Data Sources, Imports, Datasets, Uploads and Universes browser workflows,
  persistent progress, bounded preview/block pagination and lazy OHLC charts.
- Independently hash-locked API and data runtimes. Provider SDKs, pandas and Arrow
  must not be installed into the API runtime.

User-fixed baskets, current lists and historical intervals remain distinct.
Manual evidence never grants verified point-in-time status. Current snapshots
require a source reference and cannot be back-applied before their as-of date.

## Price and calendar limitations

Yahoo OHLC with `auto_adjust=False` remains split-adjusted, not truly raw.
AKShare preserves raw/qfq/hfq explicitly and converts endpoint-specific lot
volume to shares. OpenBB/Nasdaq prices retain unknown split/dividend adjustment
semantics. Provider-native research does not imply strict corporate-action or
point-in-time correctness.

An import's coverage cutoff is **not** evidence of the upstream adjustment state
at that time. AKShare/Yahoo adjustment timestamps remain unknown when the provider
does not supply them; actual response acquisition times are recorded separately.
Retries never relabel current revised history as historical as-of data.

Nasdaq and other supported US venues use a declared XNYS regular-equity schedule
proxy, preserving the actual MIC. Shenzhen uses a declared XSHG schedule proxy.
These are not claims of authoritative equivalence for every venue or security.
The proxy note is visible in resolved and pinned listing views. Foreign-market
calendars, missing sessions, conflicting duplicates and malformed OHLCV are not
silently repaired or forward-filled into tradable bars.

## Exact native execution boundary: cold start, not warm-up-aware research

The transitional fixed-seed runner stages only rows in `[start, end)`. It does not
stage earlier rows as warm-up history, even when an immutable snapshot contains
them. AKQuant can consume pre-start history, but this adapter does not yet supply
it. The unchanged timing seeds request 20 closes; AKQuant's initial NaN padding
can make the original US timing seed select shorts before its history is full.
Cross-sectional seed history requirements likewise need explicit preflight.

Launch and result screens disclose this cold-start source behavior. Market
timezone/lot-size corrections do not imply corrected warm-up semantics. Results
remain native, strategy-and-engine-shared-process outputs, not an independently
trusted ledger or final holdout evidence.

M2's scoped data gate checks a complete provider-to-immutable-snapshot-to-original
seed execution, using its actual staged window and explicit native price
semantics. It does **not** certify full research execution. M3 must implement
immutable warm-up/window configuration, sufficient-history preflight, bounded
pre-start history staging without future-data exposure, and a measured
source-versus-corrected comparison while preserving M0 baseline evidence.
The early short-optimization vertical slice also remains incomplete until the
M3 optimizer/checkpoint gate; a seed-only success is not an optimization run.

## Reviewed fault cases

Offline review reproduced the following defects before correction and added or
checked focused regressions:

1. Identical Nasdaq identities conflicting across Yahoo/OpenBB calendar aliases.
2. Interrupted writes poisoning final content-addressed upload/checkpoint paths.
3. Whole-upload raw data multiplying across monthly checkpoints.
4. Wrong-market calendars accepting exchange holidays as valid sessions.
5. Unequal CSV record widths being silently reinterpreted as an implicit index.
6. SDK children surviving creator death before Python entry-point initialization.
7. Missing Yahoo event columns being mistaken for a verified empty event feed.
8. Retries mixing changed block policies or fixture/live deployment modes.

Publication uses complete temporary files, flush/fsync and atomic no-clobber
links. An SDK child receives its expected creator PID before launch. Immutable
server-generated execution policy pins transport, block policy and contract
versions; incompatible workers fail before provider access or checkpoint reuse.
Review also caught missing supported US proxy MICs, and a false adjustment-as-of
claim. The warm-up issue above is explicitly deferred, not reported fixed.

Passing local file/process tests does not prove PostgreSQL or browser correctness.

## Required hosted gates

Normal feature-branch pushes run deterministic fixtures. Generated provider
fixtures contain artificial prices over actual exchange sessions in
`[2024-01-02, 2024-07-01)`; they are not historical observations or live downloads.

The hosted workflow requires:

- The unchanged M0 four-task AKQuant baseline.
- Separately locked SDK-free API and complete data runtimes.
- Real PostgreSQL migrations, API transactions and worker publication tests,
  including concurrent idempotency, partial baskets, cancellation, refresh,
  cross-owner denial and API-upload interruption/retry.
- Actual owned research/data-worker SIGKILL, durable recovery, checkpoint reuse
  and stale-generation redelivery, with the data worker in its own interpreter.
- Fail-closed Docker isolation and original seed execution.
- Generated OpenAPI/SDK drift checks, frontend formatting/types/build/unit tests,
  and actual Chromium against API, database, queue and both workers, without
  browser route mocks.

Three browser scenarios are tagged `@provider-flow`: AKShare Shanghai + Shenzhen
to the CN cross-sectional seed, Yahoo AAPL to the US timing seed, and OpenBB MSFT
to the US timing seed. They exercise resolve, import, progress, reload, exact
snapshot metadata/hash, preview/chart and Docker execution. Other scenarios cover
partial failure/retry, cancel/reload, immutable refresh, uploads and universes.

Explicit live mode runs small upstream SDK probes plus these three real-stack
provider flows with `E2E_LIVE_PROVIDERS=1`; it refuses fixture provenance. Any
upstream failure remains a failed live gate, not a substituted fixture pass.
Dispatch with `provider_mode=live` where GitHub supports the workflow dispatch.
For a feature-only workflow not yet on the default branch, an explicitly marked
feature commit containing `[live-data]` requests the same live mode. Ordinary
commits return to fixture mode. This does not require changing the default branch,
repository settings, credentials or permissions. GitHub documents the
[default-branch requirement for manual dispatch](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow).

CI creates and masks disposable credentials at runtime. Browser traces, videos,
screenshots, raw source data, logs and credentials are not uploaded as artifacts.
An allowlist exporter retains only test outcomes/counts/timing. These sanitised
artifacts must be tied to the exact tested commit; missing reports are not passes.

## Observed live evidence so far

One earlier local OpenBB Nasdaq directory lookup resolved AAPL. The corresponding
historical request failed with an upstream DNS error. No local AKShare/Yahoo live
probe or successful full live application flow is claimed. No TLS verification,
trust-store or proxy bypass was used. See [OpenBB findings](M2_OPENBB.md).

Provider terms and data usage rights are separate from SDK package licenses.
No source data is redistributed in the repository or CI artifacts.

## Development runtime

Continue the PostgreSQL/API/evaluator setup in [M1 operations](M1_OPERATIONS.md).
Do not replace PostgreSQL with SQLite or the evaluator with a host fallback.
Install the separate data runtime from the repository root:

```sh
python3.12 -m venv .venv-data
.venv-data/bin/python -m pip install --require-hashes -r runtimes/data/requirements.lock
.venv-data/bin/openbb-build
.venv-data/bin/python -m script.validate_runtime_split --runtime data
```

API and data-worker processes need the same database, storage root and explicit
`DATA_PROVIDER_MODE`. A fixture import cannot be resumed as live. For deterministic
development use `fixture`; for explicitly requested upstream access use `live`.
In separate terminals under that configured environment:

```sh
.venv-data/bin/python -m workers.data dispatcher
.venv-data/bin/python -m workers.data worker
```

The data worker is a trusted orchestration process, not a candidate evaluator.
Its SDK subprocesses receive a private HOME/cache, allowlisted environment,
bounded output and deadlines, with parent-death cleanup. Database/model secrets
are not inherited by SDK subprocesses. This is not a production deployment guide.
