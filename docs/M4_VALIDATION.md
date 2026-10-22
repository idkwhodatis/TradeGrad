# M4: configured research workspace

Status: **available local gate passed; real-stack browser acceptance deferred**.
The local-first instruction of 2026-10-22 permits continuing available checks,
not treating unavailable PostgreSQL, Docker or Playwright execution as a pass.
M4 does not establish end-to-end acceptance until the prepared hosted gates run.

## Implemented workflow

- A four-step experiment wizard binds every immutable RunConfig field, exact
  dataset/universe/strategy hashes, explicit market/profile, half-open windows,
  warm-up, costs, optimizer, objective, fixture model and resource budget.
  Unsupported capabilities remain readonly; server preflight is shared with
  submission validation and performs no mutation or quota reservation.
- Owner-scoped draft recovery, navigation/back/cancel, double-submit prevention,
  exact-body idempotency and lost-response recovery. Reload renews preflight;
  it does not discard the submission identity or repeat an unknown mutation.
- Persistent run detail with checkpoint progress, immutable candidate/repair
  lineage and code diffs, training/validation evaluations, model-call evidence,
  usage/budget, paged artifacts and guarded pause/resume/cancel controls.
  Completed evidence survives reload independently of a live event stream.
- Read-only native results with bounded date/listing/section queries, extrema-
  preserving sampling, ECharts, long-table virtualization and an accessible
  show-all fallback. Metrics remain the full evaluation's metrics, not silently
  recomputed for a chart filter. Raw downloads preserve original precision.
- Two-to-four evaluation comparison explains configuration and window mismatch;
  incompatible selections cannot produce ranking or comparison charts. No
  automatic winner or investment recommendation is inferred.
- Owned, checksum-verified JSON and escaped, script-free HTML report downloads.
  Download responses enforce a restrictive CSP, no-store and nosniff. Native
  strategy output never becomes active application HTML or a trusted ledger.
- Immutable strategy upload/edit/derive with exact UTF-8 hashing, syntax-only
  parsing, same-task owned parents, concurrent-safe idempotent creation and
  owner-scoped draft/retry recovery. Editing never overwrites a saved version.
  Monaco and its worker are self-hosted lazy assets, not CDN dependencies.
- Research-aware navigation, paged run lists and readonly runtime/worker status.
  Settings do not invent account management, credentials or backup readiness.

## Review corrections

An independent review caught a stale response from a departed wizard deleting a
newer draft and its retry key. Completion now checks the mounted request identity
and clears storage only if its current draft, pending body and key still match.
Seven regressions cover edited/submitted replacement drafts, response ordering,
unmount and reload. The pre-existing real-network-interruption browser test was
also updated for the new source panel and accessible source disclosure.

An uploaded fixture explicitly declaring synthetic prices now remains marked
synthetic in its published snapshot. This is distinct from fixture model usage;
neither ordinary user uploads nor frozen SDK responses become verified live data.

## Local evidence

The aggregate local gate passed **780 tests**, with zero failures or skips in the
executed suites. One optional live-market check was deselected; the live-provider
module was excluded, not passed.

| Suite | Passed | Evidence boundary |
| --- | ---: | --- |
| API contracts and runner | 200 | No PostgreSQL socket or Docker execution |
| Baseline, native results and protocol | 124 | Actual four-task original AKQuant baseline |
| Optimizer, staging and persisted result projection | 154 | Includes 19 new actual native-result projection checks |
| Market data | 118 | Frozen provider responses and original-seed component bridges |
| Frontend | 184 | Unit, rendering and generated-client transport contracts, not a browser |

The 19 new native-result checks launch only the SHA-pinned original US time seed
and pinned baseline CSV in the existing evaluator Python environment. The research
parent remains AKQuant-free, accepts the actual bounded V2 wire, and tests the real
persisted-result projection, filtering, downloads and tamper rejection. This is
engine-to-query component evidence, not an HTTP/database/queue integration pass.

Changed Python files pass Ruff and formatting; application Python source passes
Ruff. Generated OpenAPI/client synchronization, frontend formatting, type checking
and production bundling pass. Broader formatting of unchanged M0/M2 helper files
is not claimed. Large ECharts/Monaco chunks are lazy; a bundler size warning is not
a measured browser-performance result.

All **235 PostgreSQL integration cases** and four real-application system tests
are collection-verified only. All **26 Playwright scenarios** are collection-verified only. The six new M4
scenarios include actual
configured optimization, preflight and reload, response loss after real server
commit, persisted reports/comparison/ownership, pause/resume and cancellation.
The response-loss test forwards the real request and aborts only delivery; it
does not manufacture a successful backend, queue or evaluator response. Browser
collection is not browser execution.

## Remaining boundaries

Only deterministic fixture models are enabled. A complete provider's live
availability is not proven by its frozen SDK responses. Benchmark execution is
currently unsupported and shown as unavailable, not a fabricated market proxy.
Native engine evidence is explicitly untrusted because strategy and engine share
a process. Holdout is reserved metadata and is not evaluated by optimization.
Portable trusted execution, final-test exposure and strategy-package export
belong to M5; release hardening and measured browser performance belong to M6.
No live trading, paid model request, production deployment or merge occurred.
