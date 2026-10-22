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

## M1: hosted database/isolation passed, browser gate NOT passed

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
