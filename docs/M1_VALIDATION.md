# Staged verification record

## M0: passed locally

- Commit: `933d95da035939c7cef7577f0ca41c3c98c7f7e2`.
- Final post-line-ending-normalization run: **32 passed in 13.96 seconds**.
- Command: `.venv/bin/python -m pytest tests/baseline -q`.
- Mandatory four unchanged seed strategies ran on the actual AKQuant engine,
  with cold/warm-cache comparison; independent executions matched exactly.
- These are synthetic business-day fixtures, not live exchange/provider data.
- Focused Ruff, Python compilation, shell syntax and staged whitespace checks
  passed. See `baseline.md` for semantic differences and frozen versions.

## M1: passed on the real hosted stack

The [complete M1 gate](https://github.com/idkwhodatis/TradeGrad/actions/runs/39801546496)
**succeeded on attempt 2** at exact head `f66736a1da516f6a2650318bfa4c72992ad37951`,
tree `06b8acad8e3f990e23384d4262ed2110419d4810`:

- Actual four-task AKQuant baseline: **32 passed**.
- Backend/supervisor/hygiene: **55 passed in 1.13 seconds**.
- Actual PostgreSQL/Procrastinate: **77 passed in 17.81 seconds**.
- Actual Docker kernel isolation: **11 passed in 25.50 seconds**.
- Actual concurrent queue-worker SIGKILL/reconciliation/resume: **1 passed in 50.16 seconds**.
- Frontend: **25 passed**, generated contracts/type/build/format checks passed.
- Real Chromium against the actual API, queue and evaluator: **9 passed in 1.0 minute**.

Downloaded artifacts match GitHub's SHA256 digests: M0
`7e406de62740245673a3586034c52237f416b3a3170c6700cd5e216f748cc7e6`; M1
`872ce74c661b9273c818dd9950e572b05075bce61f6b78fb927fd8ac98351bef`.
All parsed cases passed, with zero failures, errors or skips. The first attempt
failed before application setup because Docker Hub returned HTTP 500 while
pulling the official PostgreSQL image; only the failed job was retried on the
same commit. No settings, credentials or image source were changed to pass it.

This closes M1's gate and permits M2 development. It remains a synthetic,
seed-backtest application, not a finished optimizer, live-data workbench or
complete V1 release. Actual worker-crash coverage is active-attempt recovery;
stalled-queue redelivery remains covered by the separate real PostgreSQL tests.

### Earlier browser-fix run, before the additional process-crash gate

The [browser-fix hosted run](https://github.com/idkwhodatis/TradeGrad/actions/runs/39798232377)
**succeeded** on exact head `a231738f769031b4072b7bca9e8d55892dfc0555`, tree
`45d1d9e914204e836abf9b911827d1a577dcd947`:

- Actual four-task engine baseline: **32 passed**.
- Backend/supervisor/hygiene: **55 passed in 1.64 seconds**.
- Actual PostgreSQL/Procrastinate: **77 passed in 28.03 seconds**, no skips.
- Actual Docker kernel isolation: **11 passed in 37.88 seconds**, no skips.
- Frontend: **25 passed**, generated contract, type, build and format checks passed.
- Real Chromium against the actual API, queue and evaluator: **9 passed in 1.2 minutes**.

Downloaded and verified artifact SHA256s: M0
`4e175f0c05495e7396cf79787f8c5f8c2921d22ab8de43d4f7b706a8f91616c6`; M1
`10856e96ab5254845a780f9f4638e8cb9bee6e6d82671526dc393159c866407a`.
Their parsed case counts match the logs, with no failures, errors or skips.

The plan's M1 gate also explicitly requires two concurrent runs without crossed
namespaces and forced-worker-crash recovery. SQL fault-injection tests plus
container cancellation tests are not equivalent to killing actual queue workers.
The additional opt-in `tests/system/test_worker_recovery.py` gate therefore creates its own
disposable database, storage and processes, submits two real HTTP runs, observes
two running evaluator containers, SIGKILLs only the two owned worker process
groups, and requires reconciler-confirmed removal before resuming through HTTP.
It verifies separate retry namespaces, unchanged original-attempt history,
artifact hashes and cross-user denial. It uses real PostgreSQL, Procrastinate,
Docker and AKQuant, without mocks or direct business-state mutation. This new
check was still pending at that earlier point; its later passing result is recorded above.

### Earlier first hosted run and corrected browser assertion

The [first hosted gate](https://github.com/idkwhodatis/TradeGrad/actions/runs/39796648672)
ran on exact head `e5eb72e3cd234d4e4f383ba2c0dd50a8768805b3`, tree
`3bffcc345e2b5cace704d25eec2b7be1e9915c09`:

- M0: **32 passed**, including the actual four unchanged seeds.
- Backend/supervisor/hygiene: **55 passed in 1.37 seconds**.
- Actual PostgreSQL/Procrastinate: **77 passed in 22.89 seconds**, no skips.
- Actual Docker kernel isolation and four-task engine: **11 passed in 30.18 seconds**, no skips.
- Frontend: **25 passed**, contract drift, typecheck, build and format checks passed.
- Real Chromium: **8 passed, 1 failed**. The offline/retry flow recovered, but its
  final assertion selected the first strategy row (the deliberate syntax-error
  fixture) while expecting original seed source. The fix selects the named seed
  deterministically and must pass a new exact-head hosted run before M2.

Downloaded outcome-only ZIPs were checked against GitHub's SHA256 digests:
M0 `81c39eb6d7125d0e94995ab0603efcc376f620eb1bf7d38064992afe669b76ad`;
M1 `ce27e964e4ad95a7665a83f41f942fd22a34b4fdfa80cd86280c9ee2ab8e3fd3`.
Their parsed case counts and failure counts match the job logs.

### Earlier local-only preparation record

The current executor prohibits the sockets required by PostgreSQL and Chromium,
and cannot provide a permitted container-isolation execution environment. The
supported execution escalation also failed before starting the requested command.
No host security restrictions were weakened and no unsafe evaluator was used.

Locally executable checks cover supervisor contracts, adversarial failure paths,
schema/contract generation, static checks and frontend compilation. These are
useful preparation, **not** evidence of the database/browser/isolation boundary.

- 43 supervisor/security/hygiene regression tests and 12 backend contract/storage tests:
  **55 passed in 1.40 seconds** with the separate API runtime.
- Actual PostgreSQL suite: 77 cases collected and explicitly skipped because no PostgreSQL URL was provided; not executed.
- Actual Docker suite: 11 cases collected, not executed.
- Generated OpenAPI export succeeded without database access or credentials.
- Focused Ruff and Python compilation passed for backend, workers, runtime and
  integration/security tests.
- Frontend strict TypeScript and production build passed; 25 pure behavior tests
  passed. Nine real-stack Playwright scenarios were discovered, not executed.
- Final M0 regression rerun after M1 preparation: 32 passed in 16.65 seconds.
- Frontend formatting, generated-client drift, YAML parsing and every workflow
  shell block syntax check passed.

The original runtime checks, now exercised by the hosted run above, were:

1. Real PostgreSQL migration, concurrency, outbox, Procrastinate, ownership,
   durable events, fencing, cancel/recovery and artifact integrity tests.
2. Four real seed evaluations through the Docker runner plus actual filesystem,
   network, environment, memory/output/process limits and process-tree shutdown.
3. Real Playwright HTTP flows against API + PostgreSQL + queue + evaluator,
   including failures, retry, cancel, reload, persistence and cross-user denial.

The GitHub Actions workflow prepares these gates using only ephemeral test
accounts, synthetic fixtures and a locally built evaluator image. It uses no
repository secrets or paid model endpoints. Workflow preparation is not a claim
that it passed. Hosted outcomes are recorded above.

Fixture passwords and signing material are generated freshly in memory for tests,
and on the disposable hosted runner for full-stack CI. The CI job registers log
masks before use, starts authenticated PostgreSQL only on loopback, and removes
its container afterward. No literal fixture credentials or production defaults
are shipped. Exporting OpenAPI still produces the identical frozen contract.
Only an allowlisted outcome/count/timing JUnit summary is uploaded, not failure
payloads, test names/parameters, HTML reports, screenshots, traces or raw logs.

## Not run / not implemented

- Live AKShare, yfinance or OpenBB provider validation (M2).
- Configurable optimizer and frozen-model integration/checkpoint gates (M3).
- Full V1 data/experiment/comparison interface (M4).
- Portable policies, separate trusted ledger and holdout/export gates (M5).
- Production deployment, restore rehearsal and performance release gate (M6).
- Any real trading, financial transaction or paid external model request.

## After-hours history rewrite revalidation

The authorized feature-only timestamp rewrite preserved the M1 tree
`06b8acad8e3f990e23384d4262ed2110419d4810`. The new M1 head
`f2e1209e96c18de930a6fd869eedab16fd4cb191` passed the complete gate again in
[run 39861794800](https://github.com/idkwhodatis/TradeGrad/actions/runs/39861794800).
Commit metadata changed; actual GitHub push and CI timestamps did not move.
The default branch was not changed.

### Cumulative recovery observation repair (2026-10-23)

The cumulative fixture run [39936992994](https://github.com/idkwhodatis/TradeGrad/actions/runs/39936992994)
failed only the two-worker recovery observation deadline at exact source
`0a794fc8513e03884d59e0c21ba8e476027fd78c`. Immediate allowlisted diagnostics
observed both attempts durably interrupted with confirmed sandbox stops and
absent containers, without a recovery blockage. Heartbeat ages were 18.33 and
18.36 seconds. Those observations do not prove the actual finish-time deltas or
retroactively pass the failed test.

The former 18-second observation wait was exactly the configured 10-second lease
plus a 2-second reconciler cycle plus two serial 3-second stop grace periods. It
left no allowance for Docker CLI, database commit, HTTP observation or scheduling.
The harness now permits a separate 4-second observation allowance (22 seconds
total), followed by exactly one full predicate observation to close the last-sleep
race. This allowance is not a measured worst-case or a production SLO. The global
120-second test deadline, real SIGKILL, immutable attempt identity/history, durable
interruption, container absence and strict recorded stop-before-20-second-natural-
completion checks are unchanged. Production timing and the slow seed are unchanged.
Pure fake-clock/AST regressions are not the required hosted recovery acceptance;
the repaired exact head must still pass the real gate.
