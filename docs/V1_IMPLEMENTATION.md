# WebUI V1 implementation and release gates

Full cumulative original-V1 fixture acceptance passed on 2026-10-23 in
[run 39942518018](https://github.com/idkwhodatis/TradeGrad/actions/runs/39942518018), published head `42f6cc25f2e4525dcbaad4e58ee95cf733e286cd`,
Git tree `a99216d3fe10b5c5015627e78cabad1e5de66577`. Local integration source
`257c290d3e7d87fc2a479f2bb4b4d8406d42eeaa` maps to that same tested tree.
Baseline, real application and independent full Compose jobs all succeeded;
the diagnostic-only job was intentionally skipped. Live-source evidence remains
pending and is not inferred from this fixture acceptance.

## Scope

This branch implements the research workbench described in the supplied 2026-10-22 plan. It does not implement V2 broker execution, paper accounts, external engine deployment, or financial transactions.

The existing upstream research baseline is `baf73ee56353de8996731139f6721b62f4b26da7`. The original source has no root license at this revision. This branch does not invent a license or imply redistribution permission. Public product distribution requires resolving upstream licensing independently.

The application adapts the MIT-licensed FastAPI full-stack template, pinned to `b1125d8839482cb7bbae603ddc6d0081bf87532e`. The API runtime and evaluation runtime are pinned separately rather than forcing quant dependencies onto the template's newest Python runtime.

## Sequential acceptance gates

No stage is called complete solely because code was written. Freeze the gate evidence, commit the validated stage, and then start the next stage. Tests must exercise real engine, database, HTTP, worker, and browser boundaries where specified. Network provider probes are separate from deterministic CI fixtures. Fixture verification is never represented as upstream live-data verification.

1. **M0: Source and semantics.** Four original CN/US time/cross tasks execute against small frozen data through AKQuant. Explicit market propagation, bounded process concurrency, warning/nonzero/missing-result judgement, and content-based cache identity have regression tests. Record versions and differences from upstream.
2. **M1: Application and reliable execution.** PostgreSQL, migrations, authenticated users and resource ownership, run/attempt separation, atomic outbox handoff to Procrastinate, idempotent submission, isolated per-attempt storage, resource limits, fail-closed candidate isolation, complete process-tree cancellation, fencing and recovery. Establish a tested vertical slice before further features.
3. **M2: Data and snapshots.** AKShare, yfinance and OpenBB Nasdaq adapters independently resolve, download, normalize, validate and publish immutable snapshots. Preserve provenance, action/price semantics, coverage, raw data, Parquet, and exact content hashes. Cover partial errors, retry, uploads, refresh, calendar boundaries and non-PIT universes.
4. **M3: Research and evidence.** Configurable TradeGrad execution, recorded deterministic model responses, immutable candidate/repair lineage, ordered events, atomic compatible checkpoints, bounded model/runtime budgets and full ResultBundle exported before the engine result disappears.
5. **M4: Browser workflows.** Authenticated datasets, sources, universes, strategies, experiment wizard, run detail, results, comparison and settings. Run generated OpenAPI contract checks, type checking and Playwright against the real API/database/worker. Verify interrupted, repeated, cancel/back/reload and persistence paths, not only happy paths.
6. **M5: Portable and selection.** Two explicit policy contracts, trusted execution ledger separate from strategy process, observable traces, prefix/future perturbation tests, experiment-family holdout exposure, and reproducible strategy bundles. Do not claim external framework equivalence.
7. **M6: Release hardening.** Cross-user access protection including events/downloads, kill-at-boundary recovery, resource ceilings, restore rehearsal, deployment/upgrade instructions and measured local performance. Record all unrun live checks and deployment limitations.

## Publication

Keep phase commits separate. No merge, deployment, paid model call, new credentials or PR-comment replies are included in validation. Verify artifact hashes and the exact branch head before any separately authorized publication.

## Evidence status

M0 and M1 passed their hosted gates on exact head
`f66736a1da516f6a2650318bfa4c72992ad37951` in
[run 39801546496](https://github.com/idkwhodatis/TradeGrad/actions/runs/39801546496).
This includes the actual four-task engine, PostgreSQL/Procrastinate, Docker
isolation, two-worker SIGKILL/recovery and nine real Chromium scenarios.
Downloaded outcome artifacts were hash-verified. See
[staged evidence](M1_VALIDATION.md) and [current operations](V1_OPERATIONS.md).

M2 data integration passed its deterministic hosted gate on `0f6e8359` in
[run 39868584582](https://github.com/idkwhodatis/TradeGrad/actions/runs/39868584582). See
[M2 acceptance and limitations](M2_VALIDATION.md), including the transitional
cold-start seed behavior at that historical stage. The later complete three-adapter
short-optimization slice is implemented; its cumulative fixture evidence is
tracked separately in the verified PC record below, not inferred from M2.
M3 passed the complete deterministic hosted gate at
`5f9125caa47107824f75094dcec08b799a829b8f` in
[run 39870319437](https://github.com/idkwhodatis/TradeGrad/actions/runs/39870319437).
This includes actual upstream TradeTGD, configured native warm-up, real
PostgreSQL/Docker optimization, recovery and Chromium. See [M3 evidence](M3_VALIDATION.md)
and the [separate research runtime](../runtimes/research/README.md). The earlier
local-first permission allowed preparation, not silently passing unavailable gates.
M4's available local gate passed 780 tests, including real native-result-to-query
component checks and 184 frontend contracts. Its complete browser/queue/database
acceptance subsequently passed on exact repaired head
`4dba3edc515a99345de806679f64091faddb8dd1` in
[run 39875412249](https://github.com/idkwhodatis/TradeGrad/actions/runs/39875412249).
See [M4 evidence](M4_VALIDATION.md). M5 passed its normal complete hosted gate on
`5bf831f63fa0daf406a355ae99f09d3b9b471be1` in
[run 39903672696](https://github.com/idkwhodatis/TradeGrad/actions/runs/39903672696),
including 297 PostgreSQL, 52 paired-container and 28 Chromium cases, both required
real worker recoveries and all native/portable final application flows. See
[M5 boundaries](M5_VALIDATION.md) and [hosted checkpoint](M5_HOSTED_CHECKPOINT.md).
M6 subsequently passed on `32fe1252fc27b6fe4d7cbee1ae3cebcff3c02495` in
[run 39907196795](https://github.com/idkwhodatis/TradeGrad/actions/runs/39907196795),
including private cold backup/empty-target restore, genuine M3-to-M5 migration,
28 Chromium cases and both bounded reference/concurrent-workload reports. These
synthetic loopback measurements do not establish production capacity or replace
the later six-year daily reference. See [operations](V1_OPERATIONS.md),
[M6 evidence and remaining gates](M6_VALIDATION.md),
[failure matrix](M6_FAILURE_MATRIX.md) and [restore profile](M6_BACKUP_RESTORE.md).
The original-V1 model admission completion is a distinct subsequent slice:
disabled-by-default, owner-allowlisted OpenAI-compatible profiles for native
research, bounded trusted SDK subprocesses, and durable process-stop receipts.
It retains upstream TradeTGD and historical fixture configuration bytes. Its
synthetic SDK/application and profile-browser gates passed on
`4550134750d6369701e996aeeabb9eb2572cacd6` in
[run 39911259617](https://github.com/idkwhodatis/TradeGrad/actions/runs/39911259617),
together with the complete inherited application/browser/performance gate. This
is separate from real-provider acceptance. A separately reviewed portable
extension now admits the same
operator profile boundary; see [portable admission](PORTABLE_OPERATOR_ADMISSION.md).
See [operator model boundaries and evidence](V1_OPERATOR_MODELS.md).
Live provider verification is separate from deterministic
synthetic SDK-response fixtures. An absent or pending report means the relevant
phase has not passed. The [current acceptance matrix](V1_ACCEPTANCE_MATRIX.md)
tracks all 103 plan requirements and supersedes historical stage omissions.

## Original-V1 cumulative fixture evidence: accepted PC

Accepted fixture source: `42f6cc25f2e4525dcbaad4e58ee95cf733e286cd`; Git tree
`a99216d3fe10b5c5015627e78cabad1e5de66577`; [run 39942518018](https://github.com/idkwhodatis/TradeGrad/actions/runs/39942518018).
Status: **full cumulative fixture acceptance passed**. Baseline, real application
and independent Compose jobs succeeded. The diagnostic-only job was intentionally
skipped; it was not a required acceptance group. All independent teardown
boundaries, ordinary-browser owned-process/container cleanup, Compose cold restart
and final evaluator absence were verified. The preserved artifact identities and
suite counts below belong to this exact run.

The bounded PC scope is the original four-task engine, actual PostgreSQL and
Procrastinate, native/portable kernel isolation and worker recovery, configured
original TradeTGD, authoring/contracts/preflight, benchmarks and persisted
all-mode result exploration, owner/admin Settings and operational observations,
ProviderConnection revisions and delayed-worker frozen bindings, both dedicated
synthetic operator-model lifecycle/browser groups, ordinary Chromium, cold
backup/empty-target restore/upgrade, cold cleanup followed by exact-checkpoint
resume, and independent full Compose browser/restart/owned cleanup.

### Exact fixture outcome counts

These are separate suite counts, not a deduplicated total. “Passed” excludes
skips; opt-in/profile skips do not stand in for their separately required groups.
The dedicated native/portable synthetic operator groups passed independently of
the two ordinary Chromium profile skips. The process-recovery selector executed
both required recovery cases; its 51 filtered opt-in cases were not recovery passes.

| Job / outcome file | Passed | Skipped | Failures / errors |
| --- | ---: | ---: | ---: |
| Baseline: `m0-junit.xml` | 32 | 0 | 0 / 0 |
| Baseline: `m3-native-junit.xml` | 92 | 0 | 0 / 0 |
| Application: `junit.xml` | 45 | 2 | 0 / 0 |
| Application: `m1-isolation-junit.xml` | 11 | 0 | 0 / 0 |
| Application: `m1-m2-postgres-junit.xml` | 442 | 16 | 0 / 0 |
| Application: `m1-m2-process-recovery-junit.xml` | 2 | 51 | 0 / 0 |
| Application: `m1-supervisor-junit.xml` | 1603 | 0 | 0 / 0 |
| Application: `m2-data-contracts-junit.xml` | 167 | 0 | 0 / 0 |
| Application: `m3-configured-application-junit.xml` | 2 | 0 | 0 / 0 |
| Application: `m3-research-contracts-junit.xml` | 343 | 0 | 0 / 0 |
| Application: `m3-window-engine-junit.xml` | 27 | 0 | 0 / 0 |
| Application: `m5-native-final-application-junit.xml` | 2 | 0 | 0 / 0 |
| Application: `m5-portable-application-junit.xml` | 2 | 0 | 0 / 0 |
| Application: `m5-portable-calendar-junit.xml` | 117 | 0 | 0 / 0 |
| Application: `m5-portable-contracts-junit.xml` | 955 | 1 | 0 / 0 |
| Application: `m5-portable-final-application-junit.xml` | 4 | 0 | 0 / 0 |
| Application: `m5-portable-isolation-junit.xml` | 52 | 0 | 0 / 0 |
| Application: `m6-backup-upgrade-junit.xml` | 16 | 0 | 0 / 0 |
| Application: `v1-cold-recovery-application-junit.xml` | 1 | 0 | 0 / 0 |
| Application: `v1-operations-application-junit.xml` | 1 | 0 | 0 / 0 |
| Application: `v1-operator-model-application-junit.xml` | 18 | 0 | 0 / 0 |
| Application: `v1-portable-operator-application-junit.xml` | 9 | 0 | 0 / 0 |
| Application: `v1-portable-upload-admission-junit.xml` | 62 | 0 | 0 / 0 |
| Application: `v1-provider-research-application-junit.xml` | 9 | 0 | 0 / 0 |
| Application: `v1-result-exploration-application-junit.xml` | 1 | 0 | 0 / 0 |
| Application: `v1-strategy-authoring-application-junit.xml` | 2 | 0 | 0 / 0 |
| Independent Compose: `v1-compose-source-junit.xml` | 69 | 0 | 0 / 0 |
| Independent Compose: `junit.xml` | 45 | 2 | 0 / 0 |

### Preserved exact-run artifact identities

| Artifact | ID | Archive SHA-256 |
| --- | --- | --- |
| Baseline | `12497110883` | `6003f47502a10c54e6d83f9378ae12c378b15f24ec5c6ec4defc43d52a60ca26` |
| Application | `12498911456` | `a2cc9aec19aa11447e780ef54393ba0f61d32366f383d2d5e9b815549957f5f3` |
| Independent Compose | `12497710144` | `0e39e74c9610d227ccf312a38645c665c590d1d36161666e70090f879fff07d0` |

The daily raw report SHA-256 is
`171b6f86445765abc86ad4f35470e3db27ee79b6bb1678eedad6a616459996da`;
its separate budget receipt SHA-256 is
`902d9c881bc1f77220ae423157c26133e49997f56caaee8e3a95a6fe1f38c5c0`.
The frozen budget SHA-256 remains `60d5256bab7f8f0050ed8329f2c282b81da5b5cfbe78a351678ce4b67f52eeb8`.
These outcome artifacts and hashes were verified against the exact run; raw
private logs, credentials, database archives and source data are not acceptance
attachments. See [daily measured guardrails](V1_DAILY_REFERENCE_PERFORMANCE.md)
for the nine observed values and their unchanged limits.

The six-year reference uses one synthetic listing and 1,456 sessions with actual
import/evaluations/API/chart/concurrency observations and zero model calls.
Its immutable calibration is from [run 39930596032](https://github.com/idkwhodatis/TradeGrad/actions/runs/39930596032).
PC records a separate actual report and result for all nine frozen engineering
limits. These are measured regression guardrails, not market-scale capacity,
verified worker-container reservations, an agreed production hardware profile
or a production latency SLO. Do not overwrite the calibration or budget with
later outcomes, or add overlapping suite counts into a unique-test total.

The previous cumulative [run 39936992994](https://github.com/idkwhodatis/TradeGrad/actions/runs/39936992994)
at head `0a794fc8513e03884d59e0c21ba8e476027fd78c`, tree
`fe639241966d9f2e320bc669cfe83f2b9c892f7d`, failed on the required legacy
two-worker recovery poll's 18-second timeout. Other application groups,
independent Compose and all nine engineering limits passed independently.
Subsequent interrupted/stopped/absent diagnostics for both attempts did not
prove the earlier poll and did not turn the workflow green. The narrow recovery
test repair and complete new exact-head result must retain that history.

## Separate live evidence and deliberate limits

The final live SDK plus browser profile has not run. It is separate from the
three generated-SDK/model-response short-optimization fixture paths and from
synthetic operator-model acceptance. The live browser selector runs only the
three `@provider-flow` import/snapshot/actual Docker seed-evaluation paths;
`@provider-research` short-optimization cases explicitly skip live mode.
Record each provider/path's actual outcome, endpoint, symbols/date range,
price mode, package/build, timestamp and tested head/tree/run. No fixture
fallback, unattempted-as-failed label, global live verification or fabricated
live optimization/paid-model gate is implied.

The [103-row acceptance matrix](V1_ACCEPTANCE_MATRIX.md) retains the missing
upstream redistribution license, separate market-data rights, labelled XSHG/XNYS
proxy calendars, user-declared unverified portable raw/no-actions assumptions,
admitted long-only daily/order subset and explicit unsupported capabilities.
A user declaration is not independent action coverage or a no-lookahead proof.
No real paid-model call, new credential/endpoint grant, trading, production
deployment, V2 certification or queued V1 increment is included.

Fixture evidence remains attributed to its actual tested head when later
substantive live-orchestration or documentation changes are made. Live outcomes
remain pending until their own run. Final live outcomes may be delivered in a
separate report linked to exact tested heads/runs, without a recursive result-only
commit. No later source tree inherits a pass merely from sharing this documentation.
