# WebUI V1 implementation and release gates

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
cold-start seed behavior and still-incomplete short-optimization slice.
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
See [M4 evidence](M4_VALIDATION.md). M5 portable
research, frozen selection and separate final-evaluation workflows passed the
available local gate and scoped review; real paired-container/application/browser acceptance
remains pending. See [M5 boundaries and gates](M5_VALIDATION.md).
M6 local preparation adds a private cold backup/empty-target restore and genuine
M3-to-M5 migration drill, bootstrap durability/resource regressions, and bounded
reference performance drivers. Its real restore/upgrade and measured workload
acceptance has not run yet. See [operations](V1_OPERATIONS.md),
[M6 evidence and remaining gates](M6_VALIDATION.md),
[failure matrix](M6_FAILURE_MATRIX.md) and [restore profile](M6_BACKUP_RESTORE.md).
The original-V1 model admission completion is a distinct subsequent slice:
disabled-by-default, owner-allowlisted OpenAI-compatible profiles for native
research, bounded trusted SDK subprocesses, and durable process-stop receipts.
It retains upstream TradeTGD and historical fixture configuration bytes. Its
synthetic SDK/application and profile-browser gates are separate from real
provider acceptance; portable research still admits only frozen fixture models.
See [operator model boundaries and evidence](V1_OPERATOR_MODELS.md).
Live provider verification is separate from deterministic
synthetic SDK-response fixtures. An absent or pending report means the relevant
phase has not passed.
