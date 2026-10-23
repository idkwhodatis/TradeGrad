# M6 reference-performance tooling and acceptance boundary

## Status and scope

This is repeatable **reference HTTP tooling**, not a production-capacity
certification and not a claim that M6 or the pending real M5 gates have passed.
The supplied plan §10.3 requires separate metadata API, chart-window query,
import, single-evaluation and concurrent-run measurements on agreed hardware and
representative daily data. This tool covers only the first two HTTP dimensions
plus one bounded persisted fills-page query. The remaining dimensions are
explicitly unsupported, not silently substituted with health checks or tests.
The separate [fixed fixture workload driver](M6_WORKLOAD_PERFORMANCE.md) covers
one small import/evaluation/concurrent-attempt smoke, not representative capacity.

The local implementation was checked with pure tests, including the production
research/final result projection functions and fake bounded HTTP connections.
No local HTTP server, sockets, PostgreSQL, Docker, browser, provider request,
strategy execution or AKQuant execution was used for these checks. **No actual
latency measurements or production hardware results are supplied by this
implementation.** A hosted JSON artifact is evidence only after the command has
actually run against that revision's running app. Synthetic projection tests and
fake clocks are not performance measurements.

M5 source was sealed at `fe28c5a2e85e2f06d08c34a1f43a56a62cfa747f` before this
scoped tooling work. Its real hosted gates were still pending at preparation;
this document does not promote them to passed. CI integration and other M6
hardening are tracked separately.

## Safe invocation against an already-running fixture app

Use Python 3.11+ (the checked runtime is the repository's API environment).
The tool itself needs only the standard library. It does not start, bootstrap,
seed or repair anything. The app, fixture database, persisted result artifacts,
and one disposable owner account must already exist.

The account's `E2E_EMAIL` and `E2E_PASSWORD` must already be set securely in the
process environment. Do not put passwords/tokens in command arguments, shell
traces, source files, reports or artifacts. The one login POST encodes the
credentials in memory for the local authentication exchange; its returned
bearer token stays in memory and is never written to evidence. Authentication is
not included in the latency samples and is not retried on failure.

Run inside the existing CI shell while its app and cleanup trap are still alive,
after the real result-producing fixture/browser flows have succeeded:

```sh
mkdir -p var/ci-evidence
python script/benchmark_v1.py \
  --origin http://127.0.0.1:8000 \
  --allow-result-access-records --require-result \
  --samples 20 --warmup 2 --timeout-seconds 3 \
  --max-points 200 --table-limit 50 --window-days 30 \
  > var/ci-evidence/m6-reference-performance.json
```

This command is a proposed hosted invocation, **not a record of an executed
benchmark**. Preserve only that allowlisted JSON as performance evidence, with
the hosting CI run and exact source revision recorded by the release process.
Do not upload arbitrary server responses or raw test diagnostics.

The default result kind is native research. `--result-kind final` selects the
separate native final-result routes. `--evaluation-id <owned-evaluation-uuid>`
selects an explicit existing result instead of searching. The identifier is
never printed. A repeatable operator run should pin that same existing result,
source revision, fixture/data revision and runtime image outside the aggregate
report. Portable result schemas are not supported by this tool.

### Important read-side effects

This is **not strict database-read-only execution**. It never submits research,
imports, model calls, trading, pause, cancel, retry or other workload-control
mutations. Its only POST is login, followed by allowlisted GET requests.
However, current result-bearing GET routes normally append/idempotently record
owner-wide result access/holdout exposure and commit. Research-run discovery can
also record summary exposure; final evidence reads can record final exposure.
Authentication also has normal server logging/rate-limit side effects.

`--allow-result-access-records` is the explicit opt-in for these normal result
read effects. It is intended for **disposable synthetic CI accounts**, not a
user's untouched holdout. Without the flag, only health and capability metadata
are measured and result reads/discovery are never attempted. `--require-result`
without the opt-in fails the completeness gate rather than overriding it. Never
use this benchmark to bypass or suppress the production exposure ledger.

### Fixture and API assumptions

The fixed API prefix is `/api/v1`. A successful login returns an `access_token`
and `token_type=bearer`. The account must own:

- For research: at least one `succeeded` native `/research-runs` entry and a
  successful, stopped, completed evaluation with a valid persisted ResultBundle
  admitted by `/research-evaluations/{id}/results`.
- For final: a `succeeded` native `/final-evaluations` entry and final evidence
  admitted by `/final-evaluations/evaluations/{id}/result`, including the existing
  final commitment/selection/admission fences. The benchmark does not alter them.
- The immutable dataset referenced by that result, so its safe aggregate
  dimensions can be read through `/datasets/{id}` when available.

Default discovery examines only the first 10 owner-scoped runs and first 20
evaluations of each successful run, with at most 5 result probes total. These
are first-page bounds, not a claim to have searched the complete history. An
older valid result outside them requires an explicit ID or adjusted bounded
flags. Discovery picks the first valid result in the server's ordering, not the
highest-scoring result. A probe must contain a nonempty equity table. No fake
fallback result is created. Missing, rejected, malformed or empty result
availability is reported as incomplete, and `--require-result` cannot pass on
metadata-only evidence.

The tool validates the selected result schema, success status, evaluation
binding, requested table keys, row-count consistency and requested row bounds.
The existing API still owns resource ownership, persisted artifact integrity and
final-admission validation; this client does not replace those checks.

## What the JSON measures

Each endpoint has separate attempts, successes, error counts, sanitized error
categories and successful-sample p50/p95/max in milliseconds:

1. `health_live`: `/health/live`.
2. `health_ready`: `/health/ready`, including the API's database readiness check.
3. `metadata_research_capabilities`: authenticated `/research-capabilities`.
4. `persisted_chart_window`: the selected existing result's `equity` and
   `drawdown` sections, filtered to the last 30 calendar days of its half-open
   evaluation window, clipped to that window, with at most 200 points/section.
5. `persisted_fills_page`: the same date window, `fills` only, offset zero and at
   most 50 returned rows. An empty fills page is valid and its zero size is shown;
   it must not be described as a busy trade-history workload.

The configured point, row and calendar-day caps can be changed within the CLI's
finite limits. Calendar days are not trading sessions. Table dimensions include
only total, filtered and returned row counts from an untimed observation, never
the rows or their contents. The API still loads/validates its persisted bundle
before filtering; a small returned page is not evidence of cheap underlying
storage work.

All samples are sequential, with HTTP concurrency exactly one and a fresh
connection per request. These are client wall-clock intervals including local
HTTP, body receipt, JSON parsing and the client's bounded schema checks. There
is no keep-alive comparison, concurrency stress test, request throughput claim,
engine parallelism claim or model request in these timings. Background worker or
other host activity can still influence them and belongs in the operator record.

Warmup requests are excluded from latency statistics and have their own error
counts. Login, discovery/probes, dataset metadata retrieval and one table-size
observation per result query are also excluded. p50/p95 use linear interpolation
at `(n - 1) * percentile`; a single success has identical percentiles, and no
successes produce null values. Failed requests never masquerade as successful
latency samples. This short reference run does not establish statistical tail
confidence, a cold-cache measurement or a capacity limit.

Any warmup or measured request error makes the report incomplete. Exit status is
0 for a complete requested run, 1 for incomplete measurements/result selection,
and 2 for invalid configuration, authentication failure or an unrecoverable
client failure. A partial report can still contain useful completed groups. No
latency SLO, arbitrary threshold or maximum production capacity is asserted;
`performance_threshold` is null. Request caps are safety controls, not measured
performance limits.

### Hardware and workload provenance

`host` reports observations of the **benchmark client**, not unverified API,
PostgreSQL or worker hardware: Python version, allowlisted OS/architecture,
logical CPU count, process affinity count, physical memory if available, and
cgroup-v2 CPU quota/memory ceiling if available. A physical host count is not a
guarantee of dedicated runner resources. Missing observations are null. If
services run in different containers/hosts, do not apply this client description
to them; server/worker hardware and peak CPU/RSS remain unmeasured here.

Dataset dimensions are allowlisted numeric observations from persisted dataset
metadata: manifest `row_count`, listing count (or evaluator symbol-mapping
count), dataset calendar-day span and the result's synthetic flag. Metadata is
not independently re-counted against raw data. Missing manifest dimensions stay
null. Optional `--declared-dataset-bars` and `--declared-dataset-listings` are
explicitly marked `declared_not_verified`, never substituted for observations.
The tool does not independently establish daily frequency, completeness, PIT
coverage, representative scale, engine concurrency or deployment equivalence.

### Persisted timings are not new benchmarks

For a result found by discovery, an available timezone-aware ordered
`Evaluation.created_at` to `finished_at` interval is recorded separately as
`registration_to_completion_seconds`. Source inspection shows registration
precedes sandbox execution and completion follows evidence/artifact handling.
Thus this interval includes launch/scheduling/publication overhead: it is neither
API latency nor isolated AKQuant execution time, and has not been independently
remeasured. Explicit-ID mode does not fetch unrelated evaluation detail just to
manufacture this field; absent timestamps yield unavailable.

Imports expose creation/update timestamps that do not isolate provider fetch,
normalization, retry or execution. They are deliberately not converted into an
“import benchmark.” Import execution timing, independently timed single
assessment, concurrent-engine runs, model latency and server resource peaks are
explicit unsupported measurement dimensions. Concurrency of HTTP requests,
should a separate tool measure it, would still not certify concurrent-engine
capacity.

## Bounds and disclosure controls

- Only literal loopback HTTP origins are accepted: IPv4 loopback or `::1`.
  No DNS names (including `localhost`), remote origins, credentials, fragments,
  query strings, non-root base paths, proxies or scoped IPv6 addresses.
- Only explicit GET routes and the login POST are allowed. All 3xx responses
  fail without following `Location`, so credentials/tokens cannot be redirected.
- Default socket timeout is 3 seconds (allowed 1–10). A monotonic deadline also
  checks the bounded body-read loop. The socket timeout applies to operations;
  it is **not a claimed strict whole-process deadline** against pathological
  slow headers. The standard HTTP client bounds header lines/count. Hosted CI
  should retain its enclosing job/step wall-clock timeout as the outer boundary.
- Default body cap is 2 MiB (allowed 1 KiB–8 MiB), checked both against declared
  Content-Length and streamed bytes. Unsupported compression, invalid JSON,
  oversized/incomplete bodies and malformed success shapes fail closed. Full
  HTTP error bodies are not read for diagnostic messages.
- Per endpoint there are at most 100 measured and 10 warmup requests. Discovery
  accepts at most 20 runs, 100 evaluations/run and 10 result probes. There are no
  retries, infinite pagination, SSE streams or unbounded downloads.
- Evidence uses only fixed labels, allowlisted enums, validated counts and
  aggregate timings. It excludes source code, rows, account identities, raw
  endpoint URLs, result/run/dataset IDs, request bodies, bearer tokens, raw server
  errors and traceback text. Do not change this to dump response JSON for debug.

## Verification and remaining operator acceptance

Run only the pure unit/projection contracts in an environment where actual local
services or execution are unavailable:

```sh
.venv-api/bin/python -m pytest backend/tests/test_benchmark_v1.py -q
.venv-api/bin/ruff check script/benchmark_v1.py backend/tests/test_benchmark_v1.py
.venv-api/bin/ruff format --check script/benchmark_v1.py backend/tests/test_benchmark_v1.py
```

The tests exercise the actual parser, percentile/warmup/error accounting,
production result projections, bounded transport adapter with fake connections,
HTTP failures/redirects, origin restrictions, timeouts, missing/empty results,
unknown hardware/data dimensions and output redaction. They do not establish
live HTTP performance. Preserve the real hosted result as separate evidence.

Before release, the operator still needs to agree the representative hardware,
CPU/memory budgets, data frequency/date span/listing count/bar count, concurrent
run levels and workload/strategy complexity. On that fixed setup, separately
measure import and native evaluation paths and concurrent-engine runs, retaining
source/runtime/data provenance, successful results, failures and resource peaks.
Keep model-request time outside API latency. Set final budgets/capacity only
after those observations and acceptance review. A small CI fixture run, even
when green, does not close these plan §10.3 production-capacity acceptance items.

### Transport deadline qualification

Both benchmark transports retain the connected socket for an absolute watchdog
through response headers, detached `Connection: close` bodies and chunk framing.
They recheck elapsed time after reads and JSON decoding, reject premature EOF,
and disarm/join timers before returning. Pure fake-socket/parser tests cover
these paths without opening sockets. Python scheduling, CPU-only parsing and
the bounded 0.1-second watchdog join are not an exact real-time process-kill
guarantee; retain the enclosing CI wall-time limit. No request deadline is a
production latency SLO or a measured capacity claim.
