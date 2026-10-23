# M6 release hardening: evidence and remaining acceptance

Status: **local implementation and review; exact-head hosted acceptance pending**.
This stage is not a claim of production readiness, live-provider acceptance,
portable runtime certification or complete V1 delivery. It builds on the sealed
M5 implementation plus the separate M4 cache-header/fixture/privacy repair.
The original-V1 operator-configured model-admission completion is a distinct
subsequent slice; M6 does not silently turn its fixture model into a live provider.

## Scope

- Private cold backup and empty-target restore use official PostgreSQL clients,
  exact database/storage inventories, verified immutable bytes and explicit
  operator quiescence and SQL-trust acknowledgements. Only supported dedicated
  databases and new targets are accepted. See [the bounded profile](M6_BACKUP_RESTORE.md).
- A hosted drill creates two individually owned disposable databases, migrates
  the source genuinely to `003_m3`, creates historical records/bytes, backs up,
  restores, and applies the real `004_m5` migration. Historical records are
  synthetic fixtures, not new execution of old strategy code. Preserving a
  paused checkpoint does not establish that its original image remains usable
  or that a full restored browser/resume workflow has passed.
- Bootstrap publication now uses the shared durable no-clobber protocol. The
  bounded ASGI request accumulator rejects an oversized incoming chunk before
  copying it. [The failure matrix](M6_FAILURE_MATRIX.md) separates pure contracts,
  actual filesystem bytes, PostgreSQL, real processes, Docker and Chromium.
- Two opt-in hosted drivers measure authenticated metadata/windowed-result HTTP
  and one fixed synthetic import, one native seed evaluation, and two additional
  overlapping native attempts. Measurements are generated only by a real run.
  They do not establish production capacity, representative market scale, CPU
  overlap, provider latency or a real-time deadline guarantee.
- [Operations instructions](V1_OPERATIONS.md) document the runtime split, explicit
  durable storage provisioning, fail-closed containment, shutdown, recovery and
  upgrade. No deployment or security-setting change is performed by this stage.

## Required hosted gate

The exact published tree must pass the cumulative baseline, PostgreSQL,
Procrastinate, native and paired portable Docker, worker SIGKILL/recovery,
configured research, separate final-evaluation and real Chromium gates before
this stage can advance. In addition it must execute:

1. `RUN_BACKUP_RESTORE_DRILL=1` with `tests/integration/test_backup_restore.py`,
   using the official matching-major PostgreSQL clients. A missing executable,
   database URL or required capability fails the opted-in gate, not a skip.
2. `script/benchmark_v1.py` with result reads explicitly acknowledged and a real
   persisted result required. Health-only success is insufficient.
3. `script.benchmark_v1_workloads` with explicit disposable fixture mutations,
   a second owned research worker, actual stopped-container receipts and positive
   completed-attempt interval overlap. Merely submitting two queued runs fails.
4. Confirmed cleanup of the exact process groups started by this hosted step,
   followed by its job-owned evaluator containers. Uncertain stop cannot turn
   an otherwise successful test invocation into a passing shell step.

Only sanitized outcome XML and fixed-schema aggregate performance JSON are
retained. No database dump, backup directory, credential, browser trace, arbitrary
service log, provider response or private source data is uploaded. Raw failure
diagnostics remain private to the disposable runner and must not be copied into
public issues or reports.

## Available local gate, 2026-10-23 UTC

These suites overlap and must not be added into a unique release-test total.

| Checked scope | Observed outcome | Boundary |
| --- | --- | --- |
| API runtime: `backend/tests tests/runner tests/portable`, excluding `test_research_result_protocol.py` | 1,853 passed, 69 skipped | Pure/component contracts and actual temporary file fault tests. Includes unrun opt-in portable Docker cases; not a database or kernel-isolation pass. |
| Research runtime: `tests/tradegrad tests/portable`, excluding native result/engine bridge files and portable isolation | 1,085 passed, 1 skipped | Actual upstream optimizer with frozen responses and inert evaluator receipts, not a full application/engine run. |
| Data runtime: `tests/marketdata tests/portable/test_engine.py`, excluding live probes | 216 passed, 3 skipped, 1 deselected | Installed SDK parsing and canonical-calendar/fake-broker contracts, not live source or native execution. |
| Frontend Vitest, unchanged frontend tree | 368 passed in 21 files | Rendering/protocol contracts, not Chromium. Type checking and production build also passed. |
| PostgreSQL/system collection | 304 collected | No local PG or system integration execution. |
| Playwright collection | 28 collected in 6 files | Collection only; actual browser acceptance is a hosted gate. |

OpenAPI/client regeneration produced no diff. Frontend formatting, scoped Python
lint/formatting, whitespace checks and all 35 workflow shell blocks passed.
Runtime-split validation confirmed 49 API, 105 data and 55 research pinned package
versions. Existing Starlette, calendar/SDK deprecations and chart/editor build-size
warnings remain; they are not represented as performance measurements.

Independent review verified the explicit libpq target, default-ACL/public-schema
emptiness checks, immutable unlinked restore spool and exact owned CI cleanup.
It found and repaired the exited-wrapper/inherited-stdout cleanup edge case;
248 backup/client pure tests then passed, including bounded descendant-exit
uncertainty. A separate benchmark review repaired absolute socket watchdog and
premature-EOF cases; its 181 pure tests are included in the broader gate above.
Neither review ran PostgreSQL, Docker, sockets or a new strategy/policy locally.
The final pre-publication hygiene pass replaced literal disposable test-password
and URL-userinfo values with runtime-generated material, used fixed test IDs,
and avoided including credential values in equality-assertion diagnostics.
No real credential or new persistent access was created or configured.

One initial overbroad API-runtime invocation included the separately assigned
native engine bridge and hit 16 setup errors because the lean API environment
intentionally does not install pandas. The correct split above passed without
adding quant packages or suppressing engine assertions; that bridge remains in
the hosted baseline job's appropriate engine environment.

## Interpretation and remaining limitations

An exact-head hosted pass establishes only the tested single-host Linux profile
and finite fixtures. It does not certify power-loss durability, hot backup/PITR,
administrator-hostile SQL, public Internet exposure, multi-host operation,
production capacity or broker trading. The full backup remains a sensitive
operator artifact; checksums establish integrity, not authenticity.

Public AKShare, yfinance and OpenBB/Nasdaq probes must be reported independently
from frozen SDK fixtures, including failures and unverified scopes. The XNYS
schedule used for XNAS stays labelled as a proxy. No real model account or paid
request is used; a synthetic HTTP model server is not real-provider acceptance.
The upstream root license remains unresolved and is not invented by export or
documentation. V2 and external-framework equivalence remain outside scope.

See [reference HTTP methodology](M6_PERFORMANCE.md) and
[fixture workload methodology](M6_WORKLOAD_PERFORMANCE.md) before interpreting
any later numerical artifact. The reference run must be associated with its
actual source revision and CI run, not an earlier stage's passing report.
