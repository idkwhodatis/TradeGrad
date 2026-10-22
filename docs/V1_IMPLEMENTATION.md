# WebUI V1 implementation and release gates

## Scope

This branch implements the research workbench described in the supplied 2026-10-22 plan. It does not implement V2 broker execution, paper accounts, external engine deployment, or financial transactions.

The existing upstream research baseline is `baf73ee56353de8996731139f6721b62f4b26da7`. The original source has no root license at this revision. This branch does not invent a license or imply redistribution permission. Public product distribution requires resolving upstream licensing independently.

The application skeleton will adapt the MIT-licensed FastAPI full-stack template, pinned to `b1125d8839482cb7bbae603ddc6d0081bf87532e`. The API runtime and evaluation runtime are pinned separately rather than forcing quant dependencies onto the template's newest Python runtime.

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

M0 is locally validated at `933d95da035939c7cef7577f0ca41c3c98c7f7e2` (32 tests).
M1 is a prepared initial vertical slice, not yet runtime-gated. See
[staged evidence](M1_VALIDATION.md) and [development operations](M1_OPERATIONS.md).
M2–M6 have not started. An absent or pending report means the phase has not passed.
