# M3: immutable research execution and recovery

Status: **available local gate passed; full-stack hosted acceptance pending**. The user's
2026-10-22 instruction permits local progression and provisionally defers an
unavailable Playwright run. It does not turn missing PostgreSQL, Docker-isolation,
live-source or live-model evidence into a pass. No M3 hosted acceptance is claimed.

## Implemented boundary

- Frozen, recursively validated RunConfig: exact dataset/seed/universe hashes,
  explicit source-compatible versus corrected market semantics, costs, warm-up,
  train/validation/holdout windows, budgets, model/prompt settings and environment.
- Source-equivalent calendar-year training partitions, half-open at the adapter
  boundary and explicitly converted for original inclusive evaluator calls.
  Worst-tail reward is training-only; all required validation must be usable.
  Holdout input is unavailable to optimization, including its universe members.
- Real `src.optimizer.TradeTGD` reuse, not a new replacement optimizer. Direct
  parity tests compare sampling, prompts, generated code, ancestry, memory,
  archive order and selection across three sampling modes and revision paths.
- Versioned candidate/repair rows and per-window physical evaluation provenance.
  Each UUID is committed before sandbox creation. Output metrics alone cannot
  establish success without the matching, owned native ResultBundle artifact.
- Actual pinned AKQuant result export: equity/drawdown/cash/margin/positions,
  orders/fills/trades/fees and observed order traces. Exact input/section hashes,
  metric units and source-rounded objective remain separate. No invented score
  trace or benchmark. Native strategy and engine share a process and are explicitly
  **untrusted native** evidence, never the future portable trusted ledger.
- Bounded V2 wire transport accepts up to 8 MiB of decoded structured output,
  plus a separate ordinary-log budget. No pickle/native-object persistence,
  symlink-following output writes, overwrite, input mismatch or V1 downgrade.
- PostgreSQL-only migrations and owner-scoped APIs for candidates, evaluations,
  model calls and complete checkpoints; pause/resume and existing cancel/SSE.
  Final selection is recomputed from registered physical results and the active
  attempt's complete checkpoint, not an optimizer's claimed number.
- Separate hash-locked API, research, data and evaluator environments. Build-input
  digest plus actual image-ID pinning prevents silent runtime changes on resume.

## Recovery findings corrected during review

The review deliberately exercised failure paths beyond ordinary green tests:
validation failure cannot select a candidate; malformed but rehashed result
bundles fail validation; a resumed attempt publishes its new journal lineage
before any paid-call boundary; unknown calls stop; response/fsync/event errors
cannot fall through source retry and issue another request; a finished checkpoint
is republished in a fresh attempt without rerunning evaluations; a malformed
Docker state cannot claim a stop; exhausted budget cannot launch another sandbox;
final SSE is emitted only by database-authoritative finalization.

## Evidence classification

Local tests include real unchanged original seeds, actual AKQuant, actual source
TradeTGD with frozen responses, filesystem durability/replay, API contract tests,
frontend component tests, and explicitly fake DB/sandbox worker-boundary tests.
The latter validate orchestration signatures/order and are **not** PostgreSQL or
Docker evidence. The 2026-10-22 local aggregation passed **518 tests, with zero
failures or skips in the executed suites**:

| Suite | Passed | Evidence boundary |
| --- | ---: | --- |
| API contracts and runner | 101 | No PostgreSQL socket or Docker execution |
| Baseline, native results and protocol | 124 | Includes four original tasks with actual AKQuant |
| Research configuration, optimizer, worker and staging | 135 | Actual upstream optimizer; fake worker DB/sandbox boundaries explicitly named |
| Market data | 115 | Frozen provider responses; includes three original-seed engine bridges |
| Frontend | 43 | Unit/component contracts, not a real browser |

The research suite includes eight opt-in local integrations through the actual
worker staging function and evaluator entrypoint, four tasks times train and
validation. Only SHA-pinned, unchanged original seeds execute outside Docker in
this controlled local test. Generated candidates never receive that exception.
These checks verify warm-up bars, no pre-start orders, exclusive end boundaries,
holdout exclusion, source hashes and market timezones. The bounded worker tests
are separately fake and do not become full-stack evidence through aggregation.

Ruff, formatting, generated OpenAPI/client synchronization, frontend type checking
and production build also passed. All 210 PostgreSQL integration tests and the
two configured real-application tests were collection-verified only. The latter
cover the real HTTP/queue/Docker pipeline and a harness-owned worker SIGKILL,
reconciliation and resume against an uninterrupted fixed-response reference.
The crash check requires unchanged durable model-call identifiers/hashes, logical
candidate lineage and best selection, new physical IDs only for unfinished
evaluations, and confirmation that old child containers no longer exist.

Prepared hosted gates retain real PostgreSQL transactions, Procrastinate,
fail-closed Docker isolation, owned process-kill recovery, configured application
integration and Playwright. Their local collection/skip is not a passed gate.
The three live provider probes were excluded/deselected, not passed. Live model
requests and the new full-stack/browser gates remain unexecuted here.

## Explicit limitations

The application currently enables deterministic fixture models only; no paid
requests or credentials are created. Frozen responses preserve the seed, so no
claim of model quality, improved returns or paper reproduction is made. Real
provider availability is still subject to M2's unpassed live gate. XNYS/XSHG proxy
calendar evidence is labelled, never advertised as authoritative exchange or
historical-membership equivalence. Unsupported corporate-action/price semantics
remain fail-closed. Native arbitrary strategies may inspect staged prior/current
window data; this is not point-in-time execution enforcement. M5's separate
portable ledger and M4's research UI are not included in this M3 milestone.

## Hosted exact-head acceptance

The complete deterministic M3 gate passed at head
`5f9125caa47107824f75094dcec08b799a829b8f`, tree
`510cede360c36a0746542fb45fa29c993eb5f80c`, in
[run 39870319437](https://github.com/idkwhodatis/TradeGrad/actions/runs/39870319437).
This includes the original four-task engine baseline, configured warm-up bridge,
actual upstream TradeTGD with frozen responses, PostgreSQL/Procrastinate, Docker
isolation, worker recovery, configured optimization through the real application,
and Chromium. It does not validate live upstream market data or live model calls.
Earlier local-only counts above remain historical evidence, not the hosted total.
