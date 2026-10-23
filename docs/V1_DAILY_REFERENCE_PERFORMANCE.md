# V1 six-year daily engineering reference: measured calibration and guardrail

## Status and scope

The plan's §10.3 asks for metadata API, windowed chart, import, single evaluation
and concurrent-run measurements on representative daily data and named hardware,
followed by an agreed budget. The old 76-row smoke remains useful but is not that
reference. This addition supplies a **versioned, modest engineering reference**,
not a user-agreed production workload, hardware target, market-scale capacity,
or latency SLO. A separately frozen engineering regression budget now exists;
it does not establish whole-workflow or production acceptance.

`script/benchmark_v1_daily_reference.py` is a finite hosted-only driver. It uses
actual authenticated application APIs, the actual data worker's immutable
snapshot, and the existing configured native research/Docker/ResultBundle path.
It does not write fixture rows into business tables or introduce any provider,
API schema, strategy, model call, or product feature. Only fake-HTTP, source and
inert-parser tests have run locally. **A successful hosted calibration was
measured on 2026-10-23 in run 39930596032.** Its independent performance step and
cleanup passed, but the enclosing workflow failed four other application groups.
This is not cumulative application acceptance. The actual timings and separate
engineering guardrails are recorded below; no throughput or capacity is claimed.

Keep these historical files unchanged:

- `script/benchmark_v1.py` SHA-256
  `f55449c1cfdbf2e9dd1edb75fc3453a4dd7db1e26325a670fbb2e72cb188efad`.
- `script/benchmark_v1_workloads.py` SHA-256
  `d2853b3f790791c6425e68970b854f23018f788621fecaa7cbd3621667f001ef`.
- The existing 76-row `frontend/tests/fixtures/research-cn.csv` SHA-256
  `2ad75d287f03a5186201c1d9a1d029dd15940c8d78649c8111ae38d1a2046a1b`.

The new report is separate: `tradegrad.daily-reference-performance.v1` and
profile `cn-one-listing-six-year-v1`. Never overwrite the old smoke report or
compare their completion times as if their workload and contracts were equal.

## Frozen workload

`tests/performance/fixtures/cn-daily-six-year-v1.json` describes the reference:

- Exactly one CN/XSHG listing and 1,456 daily bars over
  `[2019-01-01, 2025-01-01)`, six calendar years.
- The 80,119-byte CSV is a deterministic triangular price series with integer
  volume and Decimal arithmetic. Its dates are the real sessions from pinned
  `exchange-calendars==4.13.2`; its prices, volumes and amounts are entirely
  **synthetic, not market data or investment evidence**.
- CSV SHA-256:
  `bae7a1e660c4db02b60fd731754de3a1b66964cee70a05850c021503f54980f3`.
- Unchanged original native `cn_time` seed SHA-256:
  `09600e5e14cb293dfd0411b697d4ff95774c045845884620d4686f11485401ba`.
- Import via `/data/uploads`, poll the real asynchronous import, then verify the
  published dataset's content hash, synthetic label, source CSV hash/byte count,
  row/listing count, exact session-date hash and complete exchange coverage.
- Publish one explicit `user_fixed` universe through `/data/universes` for that
  exact listing. It is not historical membership or PIT evidence.
- Use production immutable `RunConfig`, corrected CN profile, 20 prior warm-up
  sessions, four independent yearly train evaluations (2020–2023), and one
  full-year 2024 validation evaluation. Configured optimization requires a
  distinct validation window even when seed-only; no exception is added.
- `max_iterations=0`, `max_model_calls=0`, `max_tokens=0`, `max_repairs=0`.
  Fixture-only server capability checks precede mutations, and each finished
  run's persisted model-call list must be empty. There is no holdout, benchmark
  comparison dataset, candidate rewrite or strategy upload.
- One standalone configured run followed by an identical pair. All three POSTs
  reuse byte-identical config and the same new dataset, universe, seed and
  environment hashes. Exactly 15 physical evaluations are expected in total.

2019 supplies the history preceding the first scored session. A six-year
snapshot and five scored years exercise a multi-year daily path, but **one
listing does not establish multi-listing, whole-market, cross-sectional,
longer-horizon, optimization, live-provider or deployment capacity**.

The generator `script/generate_daily_reference_fixture.py` is pure offline
calendar/Decimal work. Run it only with the pinned data runtime to reproduce the
committed bytes. It refuses changed calendar versions or a mismatched hash; it
cannot silently revise the reference. The benchmark itself only reads the
frozen CSV and never imports the calendar package or provider SDK.

## Five separate measurements

1. **Metadata API:** 20 measured sequential authenticated `/datasets/{id}` GETs,
   after two excluded warmups, for this exact newly imported snapshot. Fixed
   count, p50, p95 and maximum use observed monotonic HTTP durations. These are
   dataset-detail responses, not a claim about every metadata endpoint.
2. **Windowed chart:** 20 measured GETs after two excluded warmups to the actual
   `/research-evaluations/{id}/results` endpoint for the new standalone run's
   2024 validation evaluation, with equity/drawdown, `[2024-01-01, 2024-04-01)`,
   maximum 200 points per series and fixed table limit 50. Every response must
   match exact run/evaluation/config/code/window identity, synthetic/trust
   labels and an unchanged persisted artifact hash. Counts, not data values,
   are emitted. The production loader reads/hash-verifies the persisted
   ResultBundle; the client never asks to rerun the engine. Normal exposure
   ledger writes remain enabled and are included in the timing and opt-in.
3. **Import:** one newly performed upload-start to ready-observation monotonic
   wall time, including upload, queue, data-worker normalization/publication and
   poll quantization. This is not a historical import timestamp or live-provider
   download measurement.
4. **Single physical evaluation:** the actually completed standalone run's
   full-year 2024 validation evaluation, measured from its newly persisted
   `Evaluation.created_at` to `finished_at`. This includes registration, staging,
   Docker startup, engine execution and artifact publication; it is neither
   isolated CPU time nor the five-window run's wall time. The report separately
   records the standalone configured run's submission-to-verified-terminal wall
   and all five physical durations in explicit year/split order. One sample
   provides no latency distribution; no p50/p95 is inferred for it.
5. **Concurrent runs:** both new configured runs are submitted before polling.
   Both must complete all five successful native evaluations with stopped
   sandboxes and the same SHA-256 evaluator image. Positive overlap is required
   for both persisted attempt intervals and at least one pair of actual physical
   evaluation lifecycle intervals. Queued requests, overlapping preparation only
   or serial child evaluations fail the gate. This proves overlapping native
   evaluation lifecycles, **not simultaneous CPU instructions**. Batch wall,
   individual configured-run walls and every physical duration stay separate.

Warm host/caches and prior suites affect the results. The standalone run and
chart reads precede the pair; there is no claimed cold-start comparison or
statistically stable engine-latency distribution. Model request time is zero
and is not added to metadata/chart API latency. API HTTP latency does include
normal authorization, artifact verification and exposure recording.

## Hosted gate and process ownership

The driver does not start services, processes, browsers or containers. The
existing disposable hosted application gate owns the API/data/research stack,
PostgreSQL and two concurrently available research workers. Run this once after
the old smoke benchmark, while the second owned worker remains alive. The
parent's process-owner integration owns shutdown and must budget the additional
bounded 600-second command. Do not run it in live-provider/operator-model
stages, run it twice through separate entrypoints, or convert failure into
`continue-on-error` success.

Required environment is the existing fixture job's generated credentials:
`GITHUB_ACTIONS=true`, `DATA_PROVIDER_MODE=fixture`,
`TRADEGRAD_REQUIRE_DOCKER=1`, `E2E_EMAIL`, `E2E_PASSWORD`. Use the API Python
runtime because it supplies the production SDK-free RunConfig/Pydantic contract.
No new account, credentials, process ownership or authorization is created.

```sh
.venv-api/bin/python -m script.benchmark_v1_daily_reference \
  --origin http://127.0.0.1:8000 \
  --allow-representative-workload-mutations \
  --timeout-seconds 5 \
  --total-timeout-seconds 600 \
  > var/ci-evidence/v1-daily-reference-performance.json
```

Exit zero requires all five dimensions, immutable evidence, zero model-call
records, both forms of actual overlap, and stopped successful sandboxes.
Failure preserves only fixed error categories and available partial aggregates.
It is not a partial acceptance or a measured production budget. If strict
concurrency fails on a host that serializes the work, preserve the failure and
review actual worker ownership/capacity; never inject sleeps into a new strategy
or fabricate timestamps to force a passing overlap.

Upload only the aggregate JSON with the exact-head test outcome evidence. Never
upload raw API bodies, credentials, tokens, account/run/listing IDs, source data,
traces or service logs. The report includes content hashes for the input/profile,
source tree, production environment, seed, new dataset/universe/config, evaluator
image, physical result payload and selected chart artifact. Hashes are
provenance, not an assertion that native strategy-process metrics are trusted.
The advertised native environment digest must also equal the checkout’s production build-input digest.
The source hash is the canonical JSON array of sorted relative path/hash pairs
for bounded production Python/migration/lock/Dockerfile sources plus the profile and CSV; its paths
and contents are not emitted. `GITHUB_SHA` is a validated optional label, not
proof that an uncommitted source tree equals that commit.

## Hardware and final-budget boundary

The report observes actual benchmark-host logical CPU count, total physical
memory, available affinity CPUs and readable client cgroup CPU/memory limits.
No hostname, raw environment, machine identifier or cgroup path is emitted.
These describe the client host, **not verified reservations or limits of the
API, data worker, PostgreSQL or evaluator containers**. Retain the enclosing
job's exact runner configuration and process-owner/runtime resource evidence
alongside the JSON. Unknown/unsupported observations remain null.

There is no production hardware promise or production SLO. The engineering
budget below does not wait for, or pretend to establish, a user-agreed production
deployment profile. Such a profile needs separate workload/hardware agreement
and measurements. A safety timeout or theoretical projection is not capacity
evidence.

## Frozen measured calibration and engineering-only budget

The original aggregate is preserved byte-for-byte at
[`evidence/v1-daily-reference-calibration-39930596032.json`](evidence/v1-daily-reference-calibration-39930596032.json),
SHA-256 `e00fe50e03d9a9715aa4ea3b11d45b1768ecf0bda788e23e90c5a0688476a259`.
Its `performance_threshold` remains null and both `capacity_claim` and
`user_agreed_production_profile` remain false. Never amend, reformat or replace
this calibration with later results.

Provenance is recorded separately in
[`evidence/v1-daily-reference-engineering-budget-v1.json`](evidence/v1-daily-reference-engineering-budget-v1.json):

- [Hosted run 39930596032](https://github.com/idkwhodatis/TradeGrad/actions/runs/39930596032),
  application job `120338031449`, downloaded aggregate artifact `12493017342`.
- Exact published commit `8115ef88b14499108e02249d7b8f5d2d616311f4`, Git tree
  `ef406e0c51511125b5c2177d1c13944edf1e4250`, verified through the enclosing run
  and saved publication records. The raw report's `identity.ci_commit` is null,
  not retroactively filled in. Its independently calculated production-source
  SHA-256 is `d27fae125c2ff34e6935255ab64959d2047a498982c37c40e59b5837f77d44b6`.
- The performance report succeeded with 15 actual native physical evaluations,
  zero model calls, successful stopped sandboxes and confirmed cleanup. Both
  persisted attempt and physical-evaluation lifecycle overlap were positive.
  Browser/operations/performance and teardown succeeded; four other application
  groups failed. Neither this record nor its checker makes that workflow green.
- Observed host: four logical CPUs, four affinity CPUs and 16,765,415,424 bytes
  physical memory. Readable client cgroup limits were null. These observations
  are not verified API/worker/PostgreSQL/evaluator-container reservations.

The chosen conservative rule is **five times each observed value, rounded up**:
`ceil(observed_ms * 5 / quantum_ms) * quantum_ms`. HTTP p95/max use a one-ms
quantum; lifecycle and end-to-end walls use a 1,000-ms quantum. Equality with the
limit passes. These generous regression guardrails are engineering choices,
not measured production limits or latency SLOs.

| Measured dimension | Calibration (ms) | Chosen maximum (ms) |
| --- | ---: | ---: |
| Metadata p95 | 3.388 | 17 |
| Metadata maximum | 3.802 | 20 |
| Windowed chart p95 | 16.189 | 81 |
| Windowed chart maximum | 19.224 | 97 |
| Import completion wall | 1,530.088 | 8,000 |
| Single 2024 validation physical lifecycle | 796.536 | 4,000 |
| Standalone five-evaluation configured-run batch wall | 6,147.653 | 31,000 |
| Concurrent two-run batch wall | 6,805.126 | 35,000 |
| Whole benchmark wall | 14,983.314 | 75,000 |

This is one calibration invocation on a warmed host after preceding suites.
Metadata and chart each have 20 sequential measured samples after two excluded
warmups; their sample p95s are not a population service guarantee. Import and
run observations include queueing and 500-ms poll quantization. The single
physical evaluation includes staging/Docker/artifact publication, not isolated
CPU time. One synthetic listing, 1,456 sessions and 80,119 CSV bytes cannot
establish whole-market, multi-listing or live-provider capacity. No additional
benchmark, cold-cache distribution, worker reservation or LLM measurement is
implied by this frozen record.

### Pure report gate and separate receipt

`script/check_v1_daily_reference_budget.py` uses only the standard library and
bounded local JSON/file work. It cannot start services, open sockets, launch
Docker/PostgreSQL/strategies/SDKs, import the production benchmark or make paid
calls. Both the immutable calibration and chosen budget are SHA-256 pinned.
Changing guardrails requires deliberate review and a new version, not silently
recalibrating against each new run.

The checker requires the exact synthetic input/profile/seed hashes, schema,
workload, measurement definitions, bounds (including polling), sample counts,
all 15 successful native evaluations, stopped sandboxes, clean completion,
positive attempt and physical lifecycle overlap, and zero model calls. It
rejects malformed/nonfinite measurements, missing evidence and a receipt passed
in place of a raw report. Input files are regular, non-symlink files limited to
64 KiB, JSON is depth/node/string bounded, duplicate keys are rejected, and
failure output does not copy arbitrary input text.

Source, image/environment and host observations may change in future exact-
profile runs. The receipt records current source/environment/image hashes,
other validated content identities, observed host, changed version fields and
external run/commit labels. It never equates unchanged labels or host readings
with a verified identical environment. A changed environment can pass these
fixed guardrails, but that result must be read with its recorded provenance;
it does not establish identical-hardware regression causality. New per-run
config/dataset/universe/artifact hashes are recorded, not required to equal IDs
from the calibration.

After the existing owned hosted benchmark succeeds, its owner can integrate
using the hosted job's API Python runtime (global `python` in the fixture job;
`.venv-api/bin/python` for this local checkout):

```sh
python -m script.check_v1_daily_reference_budget \
  --report var/ci-evidence/v1-daily-reference-performance.json \
  --receipt var/ci-evidence/v1-daily-reference-budget-receipt.json \
  --source-commit "$GITHUB_SHA" \
  --github-run-id "$GITHUB_RUN_ID"
```

`--source-commit` must be a 40-character lowercase hex provenance label and
`--github-run-id` a positive numeric label. They are not independent tree proof.
The receipt must be a **new** file: exclusive creation prevents overwriting the
raw report, calibration, budget or a previous receipt. Exit 0 means the separate
engineering guardrail passed, 1 means the raw report failed validation or a
budget, and 2 means input/receipt file handling failed. Preserve/upload the raw
aggregate and receipt separately with the exact-run job evidence; never merge
receipt thresholds into the raw measurement. Do not swallow a failing exit or
reuse a previous receipt as evidence for a new invocation. This addition alone
does not modify the hosted process owner or workflow integration.

## Finite bounds and failure behavior

- Fixed workload: at most one import, one universe and three runs; no new
  strategy, experiment or model call. At most five physical evaluations per run.
- Whole command: default/max 600 seconds, accepted 60–600. Workload ends 30
  seconds earlier to reserve cleanup. Each configured run has a 240-second
  runtime safety cap, not an accepted performance budget.
- HTTP: default five seconds, max ten and remaining deadline, with absolute
  socket watchdog, no redirect/proxy/cookies/DNS. Literal HTTP loopback only.
- Request and response bodies: default 1 MiB, accepted 128 KiB–2 MiB. A too-small
  cap fails rather than retrying unbounded. Each GET/POST route is allowlisted.
- Request count: at most 1,024, with 32 reserved for cleanup; polling is 100–1,000
  ms (default 500); seed discovery at most five pages/500 records.
- Metadata/chart sample counts, profile, dates, listing count and physical
  evaluation count are constants, not load-generator knobs.
- Cleanup cancels only this invocation's returned nonterminal import/run IDs.
  It separately checks run attempts **and child evaluation sandboxes** stopped.
  Accepted cancellation alone is not completion. Unconfirmed cleanup is reported
  uncertain and remains the enclosing job owner's responsibility.
- Lost POST responses are never retried. An unreturned import/universe/run ID
  stays an explicitly unknown mutation outcome; no broad discovery, deletion or
  kill is attempted. Completed records remain until disposable-job cleanup.
- Bounded source hashing/parser work and Python scheduling mean the watchdog is
  not an exact real-time process-kill guarantee. Keep the enclosing job timeout
  and process owner; do not treat timeout ceilings as SLOs.

## Offline verification

```sh
.venv-api/bin/python -m pytest backend/tests/test_benchmark_v1_daily_reference.py \
  backend/tests/test_v1_daily_reference_budget.py -q
.venv-api/bin/ruff check script/benchmark_v1_daily_reference.py \
  script/generate_daily_reference_fixture.py backend/tests/test_benchmark_v1_daily_reference.py \
  script/check_v1_daily_reference_budget.py backend/tests/test_v1_daily_reference_budget.py
```

The tests use fake clocks, fake HTTP and the real HTTP parser over inert memory
streams. They cover fixed source/profile hashes, all five paths and exact same
config, zero-call enforcement, dataset/source/coverage/result bindings, false
concurrency, lifecycle cleanup, lost responses, hard request/deadline/body caps,
allowlists, redaction and detached-socket watchdog behavior. They do not open
sockets or execute a strategy, provider SDK, browser, Docker or PostgreSQL.

The budget tests additionally verify the untouched calibration hash, explicit
rounding formula, inclusive thresholds, changed-version provenance, malformed
and bounded JSON, missing/failed/zero-overlap evidence, no-model requirements,
receipt separation and refusal to truncate any existing file. Running those
tests or checking the frozen calibration is offline verification, not a new
performance measurement.
