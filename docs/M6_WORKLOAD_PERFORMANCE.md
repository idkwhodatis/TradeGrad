# M6 bounded fixture workload performance evidence

This supplements `script/benchmark_v1.py`, which measures metadata/result HTTP
queries. The two reports answer different questions. This driver creates new
work through public HTTP routes and measures its completion; it never presents
old persisted timestamps as a newly executed benchmark.

## Scope and current evidence

`script/benchmark_v1_workloads.py` performs, in order:

1. One CSV upload and asynchronous import completion through `/data/uploads`,
   `/data/imports/{id}` and `/datasets/{id}`.
2. One actual `cn_time` native seed evaluation through `/runs` and `/runs/{id}`.
3. Exactly two additional native seed runs, submitted before waiting for either
   to complete. Both must succeed, and their persisted execution-attempt
   intervals must overlap by a positive duration. Serial execution fails with
   `concurrency_not_observed`; two queued requests alone do not pass.

The immutable input is the existing Playwright
`frontend/tests/fixtures/research-cn.csv`: 76 explicitly synthetic XSHG daily
bars, one listing, `[2024-01-02, 2024-04-27)`. Its SHA-256 is pinned in the driver.
The unchanged repository `cn_time` seed is selected by exact content hash,
task, original name, native mode and absence of a parent. The newly published
dataset must report 76 bars, one listing, matching dates and synthetic status.
The same new immutable dataset and seed version are reused for all three runs.
No strategies, universes, experiments or optimization jobs are created.

This is a small, reproducible smoke baseline, **not representative market-scale
capacity evidence**. There is no capacity target or extrapolated throughput.
It does not exercise public-provider fetch latency, paid or live models,
optimization, portable execution, financial trades or broker connections.
The fixed fixture and workload size are deliberately not CLI configurable.

Local validation uses only fake transport/connection objects. It starts no
sockets, database, Docker container or strategy engine. Passing those tests
proves the driver contracts, not actual import/evaluation performance. Only a
successful hosted invocation produces workload measurement evidence. Numeric
measurements are intentionally not fabricated in this document.

## Hosted invocation after the M5 gate

Run in the existing disposable real-application CI job, after Playwright and
while its actual PostgreSQL, API, dispatcher, research and data workers are
alive. Keep the existing runtime split and production Docker evaluator path.
Provision enough owned research-worker capacity for the pair: two owned worker
processes with concurrency one each, or one worker with concurrency two. The
driver does not start, reconfigure or stop services. A second worker may be
owned by the enclosing CI stage and stopped by that stage after this command.

Required environment:

- `GITHUB_ACTIONS=true`.
- `DATA_PROVIDER_MODE=fixture`.
- `TRADEGRAD_REQUIRE_DOCKER=1`.
- Existing generated job-local `E2E_EMAIL` and `E2E_PASSWORD`; no new account,
  static password or token is created by this driver.

```sh
python -m script.benchmark_v1_workloads \
  --origin http://127.0.0.1:8000 \
  --allow-fixture-workload-mutations \
  --timeout-seconds 5 \
  --total-timeout-seconds 300 \
  > m6-workload-performance.json
```

Exit zero means all three workloads passed, native execution evidence was
verified, and the pair's attempt intervals overlap. A nonzero exit preserves
allowlisted partial measurements and a fixed error category where possible.
Do not use `continue-on-error` to promote an unsuccessful result into a passing
performance gate. Upload only the generated aggregate JSON, not raw HTTP
responses, environment dumps, service logs or source fixtures.

Before the first workload mutation, the driver also checks the authenticated
server's `/data/providers` reports fixture mode for every provider and
`/research-capabilities` advertises only the deterministic fixture model and
`live_model_calls=false`. A client environment assertion alone does not pass.
The actual runs use the native seed endpoint, so model calls remain zero.

The opt-in also covers the application’s ordinary exposure/audit records
created by submitting runs and reading their result-bearing summaries. This
driver never enumerates or changes another run, and uses only its returned IDs
for progress and cancellation.

## Interpreting the JSON

- `import.completion_wall_ms`: monotonic wall time from upload request start to
  observing the import ready. Includes upload, queue, normalization/publication
  and polling delay. It excludes the following metadata verification request.
- `single_evaluation.completion_wall_ms`: one individual wall time from native
  run submission start through successful terminal observation and evidence
  verification. Includes queue, staging, Docker startup, evaluation, artifact
  persistence and polling. It is not isolated AKQuant CPU time.
- `concurrent_pair.completion_wall_ms`: two individual run completion walls;
  `batch_wall_ms` covers the pair from first submission start to both verified
  completions. No p50/p95 is inferred from these tiny workload sample counts.
- `persisted_attempt_duration_ms`: separately labeled server timestamps for
  each newly created, actually completed attempt, not a replacement for the
  monotonic measurement. `attempt_overlap_ms` establishes overlap of attempt
  lifecycle intervals. It includes preparation and finalization, not proof of
  simultaneous CPU instructions or overlapping individual engine callbacks.
- Native evidence requires one successful generation-one attempt, exit zero,
  stopped sandbox, a SHA-256 Docker image digest, the exact input/seed references
  and `native_strategy_and_engine_share_process` trust labeling. Container
  isolation does not make native metrics tamper-proof.
- `control_http_latency` contains request counts and linear-interpolation
  p50/p95/max milliseconds grouped by fixed route purpose. Those HTTP timings
  are separate from workload completion walls and do not include model time.
  Poll responses are sampled adaptively until completion, not a steady-state
  load test. Failed calls, if any, contribute their elapsed time and the report
  remains failed; do not treat partial failure data as a successful API baseline.
- `hardware` reports only logical CPU count and total host physical memory,
  not a Docker allocation, host identity or guaranteed reservation. Retain the
  companion API benchmark report and reviewed workflow/runner resource limits
  alongside this report to identify the execution setup.

There is one import sample, one standalone evaluation sample and one pair of
evaluations. There are no warmups or repetition loops. The standalone run can
warm caches before the pair, and existing Playwright activity can already have
warmed the host. These measurements are neither a cold-start comparison nor
a statistically stable latency distribution. Poll quantization and scheduler
contention apply; record them rather than claiming a final deployment capacity.

## Hard safety and resource limits

Only an HTTP literal loopback origin is allowed. DNS names (including
`localhost`), remote addresses, userinfo, URL paths, queries, fragments and TLS
redirects are rejected. HTTP uses direct connections without proxies, ambient
cookies, redirects or authentication forwarding. The request and response
route allowlists exclude data-provider imports, live fetches, strategy writes,
optimization, retry/resume, deletion and arbitrary URLs.

| Limit | Default | Accepted range / hard bound |
| --- | --- | --- |
| Whole command, including reserved cleanup | 300 s | 60–600 s |
| Workload portion | total minus 30 s | cleanup reserve always 30 s |
| Each HTTP request | 5 s | 1–10 s and remaining whole-command budget |
| Each request/response body | 1 MiB | 16 KiB–2 MiB |
| Poll interval | 500 ms | 100–1000 ms |
| HTTP calls | 1024 | fixed, eight reserved for cleanup |
| Seed-catalog discovery | 500 records | five pages of at most 100 |
| Created imports / runs / experiments | 1 / 3 / 0 | fixed |

An absolute request watchdog closes the socket, including during response
header parsing; response bodies are bounded while streaming. Provider/model
preflight and the explicit mutation opt-in are required even on loopback.
Outputs are constructed from fixed labels, booleans, numeric bounds/counts
and durations only. No account identities, credentials, IDs, configuration,
source data, traces, bodies, raw exceptions or logs are emitted. Unit-test
passwords and tokens are generated at runtime too.

On failure, cancellation is requested only for imports/runs whose IDs were
returned to this invocation and whose successful terminal cleanup has not
already been verified. Cancellation acceptance is not reported as completion:
a separate bounded read must confirm terminal state and stopped run sandboxes.
Timeouts or uncertain POST outcomes are not retried. If a POST succeeded but
its response/ID was lost, `unknown_mutation_outcome` and `cleanup_uncertain`
are true; no broad discovery, deletion or kill is attempted. Completed durable
records are retained in the disposable CI database/storage for its enclosing
job's normal cleanup, never deleted by the benchmark.

## Offline regression command

```sh
python -m pytest backend/tests/test_benchmark_v1_workloads.py -q
```

This covers runtime-generated credentials, fixture and model admission,
loopback restrictions, exact mutation counts, distinct idempotency keys,
synthetic size checks, real-evidence requirements, false concurrency rejection,
deadline enforcement, own-ID cancellation, lost-response uncertainty, bounded
transport and privacy-safe output. It is explicitly not hosted execution.

### Transport deadline qualification

Both benchmark transports retain the connected socket for an absolute watchdog
through response headers, detached `Connection: close` bodies and chunk framing.
They recheck elapsed time after reads and JSON decoding, reject premature EOF,
and disarm/join timers before returning. Pure fake-socket/parser tests cover
these paths without opening sockets. Python scheduling, CPU-only parsing and
the bounded 0.1-second watchdog join are not an exact real-time process-kill
guarantee; retain the enclosing CI wall-time limit. No request deadline is a
production latency SLO or a measured capacity claim.
