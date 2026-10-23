# M6 failure, ownership and resource matrix

## Accepted cumulative fixture evidence, 2026-10-23

The complete fixture gate passed in [run 39942518018](https://github.com/idkwhodatis/TradeGrad/actions/runs/39942518018)
at head `42f6cc25f2e4525dcbaad4e58ee95cf733e286cd`, tree `a99216d3fe10b5c5015627e78cabad1e5de66577`.
See [PC and exact suite counts](V1_IMPLEMENTATION.md#original-v1-cumulative-fixture-evidence-accepted-pc).
This evidence is for that exact source, not live upstreams or production deployment.

Historical local checks, exact P6 evidence and PC remain separately attributed.
Both required real recovery cases passed; the finite per-case ownership/resource
map, backup/restore/upgrade, result-to-browser and independent Compose gates passed.
The failed sixth recovery poll remains failed-run history, not a pass inferred
from its subsequent diagnostic. Prepared/collected/skipped cases are not promoted.
Power-loss, production saturation/deployment, V2 and licensing limits remain.

Status: **bounded cumulative fixture acceptance at PC, not production certification**.
This matrix maps implementation-plan §§10.2–10.3 to concrete evidence sources.
It does not promote M5's local stage (`fe28c5a2e85e2f06d08c34a1f43a56a62cfa747f`)
or older M1/M2 hosted reports into an exact-head M6 runtime pass. Hosted gates
must report their actual revision, execution outcome and retained evidence.
Collection, opt-in skips, source inspection and a prepared workflow are not passes.

## Evidence layers

- **C — pure contract:** schemas, ASGI messages, DTOs, fixed result bytes, fake
  broker/runner receipts, or command construction. Real byte parsing is still C;
  it does not execute a strategy or enforce a kernel boundary.
- **F — fake persistence + filesystem:** fake sessions/transactions and actual
  temporary bytes, injected write/fsync failures and owner checks. No PostgreSQL
  transaction/concurrency or power-loss claim follows.
- **PG — actual PostgreSQL:** real migrations, constraints, locking, transactions,
  queue/outbox and SQL ownership. Requires the real opted-in database fixture.
- **P — real process:** actual writer exit/SIGKILL/restart or a worker pipeline,
  not an exception raised inside one Python process. Some P gates also require PG/D.
- **D — Docker:** actual restricted containers on a supported Linux/cgroup-v2
  host. Inspected arguments and fake CLI responses are C, never D.
- **B — browser:** executed Chromium workflows against the real HTTP API,
  PostgreSQL and workers. Vitest and Playwright collection are not B.

The tables below are coverage maps, **not execution ledgers**. Only the final
local-check section records checks executed during this bounded M6 pass.

## §10.2 running and recovery

| Plan invariant | Exact coverage sources | Evidence and outstanding gate |
| --- | --- | --- |
| 1. Repeated submission creates one logical Run | `tests/integration/test_submission.py` (`test_concurrent_identical_submissions_commit_only_one_run`, rollback/key-conflict cases); `tests/integration/test_research_lifecycle.py`; `tests/integration/test_final_evaluations.py`; `frontend/tests/e2e/research-workbench.spec.ts`, `configured-research.spec.ts`, `final-test-workflow.spec.ts` | PG and B. Fake-session replay checks do not prove concurrent uniqueness. |
| 2. Concurrent same-task experiments do not share logs, caches, checkpoints or best source | `tests/system/test_worker_recovery.py::test_two_concurrent_runs_recover_after_real_workers_are_sigkilled`; `tests/tradegrad/test_optimizer_recovery.py`; `tests/portable/test_research_pipeline.py::test_native_and_portable_concurrent_calls_do_not_mutate_source_parser` | C/F for private state/parser bindings; PG/P/D for actual overlapping workers. Hosted process gate remains required. |
| 3. Interruption at code/evaluation/result/DB boundaries only resumes complete state | `tests/tradegrad/test_optimizer_recovery.py::test_continuous_equals_crash_resume`; `backend/tests/test_atomic_storage.py` (`test_real_writer_exit_cannot_poison_identical_retry` is P, other injected failures are F); new `backend/tests/test_bootstrap_durability.py`; `tests/integration/test_research_lifecycle.py`; `tests/integration/test_strategy_packages.py`; `tests/system/test_research_optimization.py::test_real_sigkill_reconciles_and_resumes_generated_candidate_without_model_replay` | Distinguish injected exception/fsync (F), actual writer exit (P), DB commit/rollback (PG), and full worker SIGKILL (PG/P/D). No new real-process or PostgreSQL run in this pass. |
| 4. Fixed model responses preserve the continuous/resumed candidate chain | `tests/tradegrad/test_optimizer_recovery.py` (`test_continuous_equals_crash_resume`, three-attempt recovery, completed-response replay); `tests/portable/test_research_pipeline.py`; `tests/system/test_research_optimization.py` | C/F use the unchanged optimizer and inert evaluator receipts, not an engine run. Actual generated-candidate recovery is PG/P/D. Paid/live future model output is not promised deterministic. |
| 5. Warning plus exit 0 may succeed; nonzero/missing/malformed result cannot | `tests/runner/test_runner_contract.py`; `tests/runner/test_security_regressions.py`; new `tests/runner/test_release_matrix.py`; `tests/tradegrad/test_optimizer_recovery.py::test_failed_process_outcome_cannot_be_usable`; `tests/isolation/test_docker_evaluator.py::test_zero_exit_without_valid_metrics_is_failed` | C covers frozen metrics, warning transport, invalid/duplicate/mixed/oversized records and inert outcomes. Real process exit and sandbox enforcement require D. A parser success alone is not a successful evaluation. |
| 6. Cancellation stops the whole process tree; an old attempt cannot publish after recovery | `backend/tests/test_attempt_containment.py`; `tests/runner/test_security_regressions.py`; `tests/portable/test_runner.py`; `tests/integration/test_lifecycle.py`; `tests/integration/test_research_lifecycle.py`; `tests/isolation/test_docker_evaluator.py::test_actual_timeout_cancel_kills_entire_container`; `tests/portable/test_isolation.py`; `frontend/tests/e2e/configured-research.spec.ts` | C/F for stop-group traversal, unknown-stop refusal and fences; PG for transactional generations; D/P for actual descendants, stale starts and sibling cleanup; B for UI acknowledgement versus confirmed stop. |
| 7. Generated code cannot obtain coordinator keys, other runs, arbitrary network or host management | `tests/runner/test_runner_contract.py`; `tests/runner/test_security_regressions.py`; `tests/portable/test_runner.py`; `tests/isolation/test_docker_evaluator.py::test_real_files_network_environment_and_kernel_limits`; `tests/portable/test_isolation.py::test_actual_policy_cannot_read_engine_ledger_dataset_secrets_or_network` | C only for allowlists/commands/role validation. D is the required security evidence. Native engine and strategy share a process and their result remains untrusted. No host execution fallback. |
| 8. No valid candidate produces an explicit terminal reason | `tests/tradegrad/test_optimizer_recovery.py::test_no_usable_strategy_and_budgets`, `test_recorded_model_failure_cannot_claim_run_success`; `tests/portable/test_research_pipeline.py::test_portable_actual_optimizer_enforces_model_budgets_without_online_calls`; `tests/integration/test_research_lifecycle.py::test_success_without_owned_bundle_is_persisted_as_failed` | C/F for budget/validity propagation; PG for durable failed lifecycle; full worker/browser behavior remains separate. Completing a loop is not usable-strategy success. |

## §10.3 product and result correctness

| Plan invariant | Exact coverage sources | Evidence and outstanding gate |
| --- | --- | --- |
| Main workflow without a terminal | `frontend/tests/e2e/data-workbench.spec.ts`, `strategy-versions.spec.ts`, `configured-research.spec.ts`, `final-test-workflow.spec.ts`, `portable-research.spec.ts` | B. Render/unit fixtures are not substitutes for these actual authenticated flows. |
| Refresh/SSE disconnect recovers progress and submission identity | `tests/integration/test_ownership.py::test_sse_replay_has_ordered_ids_and_resumes_after_last_event`; `frontend/tests/unit/behavior.test.ts`, `research-submit.test.ts`, `final-durable.test.ts`, `portable-durable.test.ts`, `portable-final-protocol.test.ts`; corresponding E2E files above | C for browser-state/protocol logic; PG for ordered durable events; B for actual reload/network interruption/lost response. Tokens must remain out of SSE URLs. |
| Persisted artifacts reconstruct graphs; units/formula versions and lineage remain consistent | `backend/tests/test_research_views.py`, `test_final_result_views.py`; `tests/tradegrad/test_result_views_native.py`, `test_results.py`; `tests/integration/test_research_view_http.py`, `test_strategy_versions.py`; `tests/portable/test_persisted_evidence.py` | F for owner-bound verified synthetic result bytes; native `test_results.py` contains actual engine bridge cases, not pure contracts and not executed here. PG/D/B gates must validate the full source-to-chart chain. |
| Other owners cannot access data, logs, events/SSE or artifacts | `tests/integration/test_ownership.py`, `test_data_imports.py`, `test_data_universes.py`, `test_research_lifecycle.py`, `test_final_evaluations.py`, `test_strategy_packages.py`; `backend/tests/test_control_response_access.py`, `test_result_access.py`, `test_strategy_package_contracts.py`; `tests/portable/test_final_loader_admission.py`, `test_final_package_reads.py` | F checks permission-before-file ordering and immutable result/admission gates. PG checks actual owner-scoped queries. B repeats the access denial using a second authenticated owner. No auth redesign or public-deployment acceptance is implied. |
| Restore database and artifacts before release; validate upgrades | `tests/integration/test_migrations.py`; `tests/integration/test_backup_restore.py::test_real_m3_backup_restore_then_m5_upgrade_preserves_history`; `script/backup_v1.py` | PG/P plus actual copied/hash-verified storage. Migration schema round-tripping alone is not restore rehearsal. The new opt-in backup/restore drill is prepared separately and is not reported executed by this matrix. |
| Separately measure metadata API, windowed charts, import, evaluation and concurrent runs on named hardware/data | `script/benchmark_v1.py`; `backend/tests/test_benchmark_v1.py`; real process/browser workloads above | C tests only the benchmark's bounds/statistics/redaction. Actual loopback HTTP timings and actual job timestamps need their own recorded run. Historical queue-to-terminal durations are not controlled import/evaluation benchmarks; model time is not metadata API latency. No production throughput/capacity budget is inferred. |

## Bounded resource review

| Boundary | Implemented ceiling/behavior | Coverage source / limitation |
| --- | --- | --- |
| Mutation request body before multipart/JSON parsing | `max_upload_bytes + 100,000` total bytes; upload setting defaults/caps at 10,000,000 bytes. POST/PUT/PATCH count actual chunks, not a trusted Content-Length. | `backend/tests/test_data_contracts.py`; new `backend/tests/test_release_limits.py` tests exact limit, fragmentation, disconnect, false lengths, error correlation and early rejection. M6 checks the incoming chunk **before** copying it into the accumulator. This does not bound server/proxy buffers, simultaneous connections or slow-client duration. |
| Immutable artifact/fixture bytes | Artifact setting defaults to 50,000,000 bytes per read; bootstrap source/fixture data is bounded at 10,000,000 per file and manifests by expected canonical size. | `backend/tests/test_contracts_storage.py`, new `test_bootstrap_durability.py`. Per-object limits are not a total owner disk quota. |
| Server pagination/chart window | Lists generally cap at 100; data/result tables at 500; research chart points at 5,000; import-block detail at 1,000. Owner lookup precedes bytes. | `backend/tests/test_research_views.py`, `test_final_result_views.py`; `tests/integration/test_data_imports.py::test_import_block_detail_is_owner_scoped_and_server_bounded`, `test_research_view_http.py`. Data-dependent PG/HTTP cost still needs measurement. |
| Concurrent owner work | Default active run/import cap is 4, configurable within 1–100. Retry/resume competes for the same quota rather than bypassing admission. | `tests/integration/test_submission.py`, `test_data_imports.py`, `test_final_evaluations.py` contain actual quota races. C/F cannot establish SQL serialization. |
| Native result transport | 8 MiB decoded V2 wire ceiling, bounded base64 representation, exactly one protocol record, finite schema, no overwrite of result paths. | New `tests/runner/test_release_matrix.py` covers malformed/missing/duplicate/mixed and both size boundaries without an engine. `test_research_result_protocol.py` includes the actual native engine bridge and is not executed here. |
| Portable sibling transport/state | Observation 4 MiB, reply 256 KiB, result 8 MiB, up to 100,000 decision sequence; finite bounded JSON; canonical state default 64 KiB with tighter complexity limits. | `tests/portable/test_contracts.py`, `test_state.py`, `test_runner.py`; new `tests/runner/test_release_matrix.py` checks each frame header rejects before body read. In-memory streams do not prove host deadlines or kernel containment. |
| Strategy export archive | Fixed entry allowlist, at most 128 entries, 32 MiB total including final ZIP overhead, 8 MiB per entry, 1 MiB source, 2 MiB environment evidence; stored deterministic ZIP, no external archive extraction. | `tests/tradegrad/test_strategy_package.py`, `backend/tests/test_strategy_package_contracts.py`; PG ownership/replay/rollback in `tests/integration/test_strategy_packages.py`. No newly uncovered archive limit change was needed. |
| Evaluation CPU/memory/PID/tmpfs/log/deadline | Native defaults: 1 CPU, 1,024 MiB memory, 64 PIDs, 64 MiB output and temporary areas, 2 MiB diagnostic logs plus bounded result transport. Required cgroup/seccomp capabilities fail closed; portable siblings have role-specific caps and wall/phase deadlines. | C command/fake-CLI tests in `tests/runner/test_runner_contract.py`, `test_security_regressions.py`, `tests/portable/test_runner.py`; D kernel tests in `tests/isolation/test_docker_evaluator.py`, `tests/portable/test_isolation.py`. Defaults are not verified production capacity or a substitute for D. |
| Optimizer/model budget | Typed ceilings for iterations, calls, tokens, runtime and repairs; uncertain or completed calls preserve journal identity across retry. | `tests/tradegrad/test_config_windows.py`, `test_optimizer_recovery.py`; `tests/portable/test_research_pipeline.py`. Frozen responses are not live upstream cost/rate-limit evidence. |

This was a finite review of the listed ingress, result, archive, frame, owner and
publication boundaries, not an assertion that every possible resource exhaustion
or hostile database-administrator mutation has been audited.

## M6 local changes and observed checks (2026-10-22)

- `backend/app/bootstrap.py` now calls the shared `publish_bytes` no-clobber/fsync
  protocol with an explicit, existing durable storage root. It publishes all
  referenced files, then the manifest, then adds the dataset row. Duplicate
  bootstrap verifies the existing manifest and every referenced file, then flushes
  the verified legacy tree and its ancestry without rewriting any bytes, instead
  of silently reusing a corrupt or previously unflushed identity.
  Missing/corrupt/type-conflicting bytes fail;
  no repair/overwrite/fallback is attempted. Native seed and opt-in E2E strategy
  definitions remain DB-only inert text; bootstrap does not execute their source.
- Interrupted publication or a failed DB transaction may leave complete,
  unreferenced individual files/snapshots. They are not usable datasets, cannot
  poison an immutable final file, and are not destructively cleaned by bootstrap.
- `backend/app/api/body_limit.py` rejects the next oversized chunk before extending
  its accumulator, preserving the existing 413/code/correlation/no-store semantics.
- **75 new cases passed** across `backend/tests/test_bootstrap_durability.py`
  (34), `backend/tests/test_release_limits.py` (22), and
  `tests/runner/test_release_matrix.py` (19). All use C/F evidence only.
- **229 targeted cases passed**, including those 75 and existing affected storage,
  body, owner/result/package, containment and native-mode contracts. Exact command:

  ```sh
  .venv-api/bin/python -m pytest -q \
    backend/tests/test_bootstrap_durability.py \
    backend/tests/test_release_limits.py tests/runner/test_release_matrix.py \
    backend/tests/test_data_contracts.py backend/tests/test_storage_provisioning.py \
    backend/tests/test_contracts_storage.py backend/tests/test_result_access.py \
    backend/tests/test_control_response_access.py \
    backend/tests/test_strategy_package_contracts.py \
    backend/tests/test_attempt_containment.py \
    backend/tests/test_native_mode_boundaries.py
  ```

  One existing Starlette/httpx deprecation warning remained. Scoped Ruff check and
  formatting passed for the two modified application files and three new test
  files. These counts overlap earlier suites and are not additive release totals.
- No PG, actual worker/process recovery, Docker, browser, socket benchmark, new
  strategy/policy or AKQuant execution was attempted in this pass. Such execution
  was unavailable/denied locally and was not retried through another host route.
  Hosted execution is independently owned; its result must be attached separately.

## Retained exclusions and release conditions

1. Require supported Linux/local Docker/cgroup-v2/seccomp and PostgreSQL; do not
   substitute SQLite, a remote Docker daemon, non-Linux host execution, or an
   unrestricted Python strategy runner. Other platforms remain unsupported until
   separately demonstrated, and permission denial is not a reason for fallback.
2. Operators must pre-provision the durable root and its ancestry on a filesystem
   honoring the required file/directory fsync semantics. The storage namespace is
   not concurrently renamed or controlled by hostile writers. Process fault
   injection does not certify power loss, disk controllers, mount options, network
   storage, backup consistency under active writers, or a restore time objective.
3. Live AKShare/yfinance/OpenBB, paid/online models, provider coverage and real
   market-data semantics remain distinct from deterministic synthetic fixtures.
   Synthetic daily CN/US results are not investment validity or paper replication.
4. TLS/public Internet exposure, production account lifecycle, shared proxy rate
   limits, production capacity, large-universe/minute data and multi-host operations
   are not accepted by this matrix. Measure representative workloads on agreed
   hardware before setting production budgets. The separate frozen engineering
   regression guardrail is not capacity; no full resource-saturation proof exists.
5. V2 remains excluded: no broker account, live orders, external execution-framework
   equivalence, deployment or reconciliation acceptance. V1 reference packages do
   not authorize public redistribution or turn portable contracts into live support.
6. A release decision requires exact-revision PG/P/D/B outcomes, the actual cold
   restore/upgrade rehearsal and honest performance evidence. A skipped, collected,
   prepared or running gate stays unpassed; do not describe this matrix as complete
   security/runtime acceptance.
