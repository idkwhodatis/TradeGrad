# M5: portable research and frozen final evaluation

Status: **implementation and available local checks; hosted acceptance pending**.
Neither the portable runtime nor its controller-T+1 profile is certified by
fixture, schema, source-inspection or fake-broker tests. No live source, paid
model, external trading framework, broker or live-trading claim is made.

## Implemented boundaries

- Two explicit SDK contracts, `PortfolioPolicy.decide` and `EventPolicy.on_event`,
  return only a decision and versioned canonical JSON state. There is no pickle,
  policy-supplied accounting result or implicit state migration. Input projection
  excludes future bars, unannounced universe changes, end-of-run hints and full
  source-wide identifiers. Warm-up is state-only with a flat account.
- The trusted AKQuant engine and untrusted policy use separate restricted sibling
  containers. Only the engine receives market data; only the policy receives
  candidate source. Neither gets a Docker socket, network or host credentials.
  Bounded frames, deadlines, actual image identities, container-role checks,
  physical evaluation IDs, attempt fences and confirmed dual cleanup are required.
  Missing capabilities fail closed; there is no host execution fallback.
- Native cash, positions, orders, executions, fees and equity remain the financial
  ledger. The portable controller keeps only intent/receipt/reservation state.
  The [v3 profile](../runtimes/portable_engine/README.md) explicitly separates native
  T+0 from application-enforced CN daily-next-open T+1. No native-T+1 equivalence
  is implied. Old profile hashes reject. Limits support only the open-marketable
  or no-touch GTC subset; intrabar-only or limit-breaching fills fail the run.
- Portable research invokes the actual upstream `TradeTGD.step` algorithm with a
  private portable-code parser binding, not a replacement optimizer or a global
  monkey patch. Frozen model responses, exact prompts, candidate/repair lineage,
  model calls, compatible checkpoints and evaluation provenance are persisted.
  `portable_return_drawdown.v1` is an explicit new objective, not the paper/native
  reward. Portable and native runs, results and preflight DTOs are distinct.
- Admission requires raw prices, a fixed declared universe, complete canonical
  sessions and known no-action coverage. The dedicated `portable_fixture` source
  is explicitly synthetic; it is not offered in live-provider mode. Existing
  adjusted or unknown-action snapshots are not silently declared portable-ready.
  The XNYS schedule used for XNAS remains an explicitly labelled proxy, not
  authoritative Nasdaq equivalence. Missing bars and action windows reject.

## Selection, exposure and final jobs

Experiment families and immutable split versions bind research before execution.
The user freezes an exact eligible checkpoint candidate, not an automatically
chosen highest score. Native and portable final evaluation are separate immutable
jobs on the declared holdout, with the selected source/manifest/parameters and
actual source environment/image pins. They never run optimizer/model steps or
write their outcomes into source research checkpoints or model history.

An owner-wide append-only exposure ledger covers admission, result/preview/report
reads and exports across experiment families and re-imported identities. Retrying
or cancelling a final submission does not erase exposure. `no_tracked_overlap`
means precisely that: historical/external usage remains unknown, and
`independent_unseen_holdout_established` stays false. Exposure is not a statistical
claim of a clean, never-observed holdout.

Only the currently committed successful logical final job, current generation,
active successful/stopped attempt and exact evaluation receipt may provide an
accepted result. Both final loaders require the original admission receipt.
Dedicated and generic downloads, reports and packages use the same ownership,
hash, lifecycle and admission gates. Metadata-only lists do not disclose metrics.
Historical physical evaluations retain diagnostic metadata, not reference-result
selection. Metric-bearing control/idempotency responses also record exposure
before commit/response. Filename alone cannot exempt arbitrary result bytes as
metadata or source code.

The native result is always labelled untrusted: native strategy and engine share
a process. Portable result trust identifies the separated ledger, but
`runtime_certified=false` and external framework equivalence `not_certified`
remain explicit until independently justified gates exist.

## Reproducible owner-scoped packages

Bounded deterministic ZIP packages pin selected source, portable manifest/state
schema/parameters when applicable, configuration, split, universe/calendar/action
references, actual captured execution environment and dependency locks, final
result/trace and exposure evidence. Repeated exports preserve exact member bytes
and hashes. Packages exclude raw market data, full upstream trees, secrets,
environment variables, model prompts/responses and logs. Heuristic credential
checks are defense in depth, not proof that arbitrary user source has no secrets.

Packages are owner-scoped research artifacts, not live deployment or external
framework adapters. Upstream root licensing remains unresolved; packaging does
not invent redistribution permission or authorize public distribution of
selected/derivative source or market data.

## Application and durability checks

The UI exposes explicit native/portable modes, immutable configuration hashes,
fixture/price limitations, preflight, bounded budgets, run evidence, frozen
selection, a separate final-test lifecycle and checksum-verified exports.
Owner-scoped idempotency keys survive lost responses and reloads; final retry uses
a distinct key. Cancel acknowledgement is not sandbox-stop confirmation. Final
testing has no pause button. Stale physical evaluations cannot keep cached final
metrics mounted after retry, cancellation or generation changes.

Artifact publication fsyncs immutable bytes and every directory entry below an
explicit pre-provisioned durable root before database reference publication.
The [storage setup](M1_OPERATIONS.md#durable-storage-prerequisite) is mandatory.
Checks cover interrupted writers, concurrent creation, hash conflicts and complete
orphans. These are process/filesystem contracts, not real power-loss certification.

## Evidence and required hosted gates

Local suites exercise parsers, state, prefix/future perturbations, fake-broker
inventory/reservation invariants, real optimizer orchestration with inert runner
receipts, final lifecycle/ownership projections, packaging, storage fault injection
and frontend protocol/render contracts. They do **not** execute new policy code
or the new AKQuant wrapper on the host. Browser collection is not browser execution.

The exact-head workflow must additionally pass:

1. Real PostgreSQL migration/constraint/outbox/exposure and owner-isolation tests.
2. Existing native Docker isolation and real SIGKILL/checkpoint recovery gates.
3. Both portable container builds with one captured environment digest, followed
   by all opt-in `tests/portable/test_isolation.py` proofs. Earliest next-session
   exits, full cross-section barriers, native partial/cancel behavior and supported
   limit fills are strict required tests, not xfails or mocked substitutes.
4. `test_portable_research_pipeline.py`: actual API, queue, data worker, frozen
   model, upstream optimizer and both containers for CN event and US portfolio.
   Its queued pause/resume case proves initial-checkpoint recovery, not replay of
   an already completed model call; the latter remains separate evidence.
5. `test_final_pipeline.py` and `test_portable_final_pipeline.py`: real source
   research, exact freeze, separate final job, verified result/package, cancellation,
   retry and retained exposure, with source research archives unchanged.
6. Real Chromium flows against the API/PostgreSQL/workers, including portable CN
   portfolio and US event, duplicate/lost submit responses, reload, cancel/retry,
   cross-owner denial and checksum downloads. Deterministic fixtures remain
   clearly distinct from live provider or paid-model verification.

The user's local-first instruction permits preparing these stages while retaining
unavailable gates. Any absent, skipped or pending hosted report remains unpassed.
No merge, production deployment or live-trading acceptance is part of M5.

## Available local gate, 2026-10-22

The following suites passed on the prepared M5 sources before commit. They
substantially overlap and must not be added into a unique test total.

| Environment and scope | Outcome | Evidence boundary |
| --- | --- | --- |
| API environment: `backend/tests tests/runner tests/portable`, excluding the native result-protocol bridge | 1,333 passed, 69 skipped | Includes 51 unrun opt-in Docker cases; optional calendar/research-runtime cases are checked separately below. No new host policy/engine execution. |
| Locked research environment: `tests/tradegrad tests/portable`, excluding native engine/result bridge files and portable isolation | 1,085 passed, 1 skipped | Actual unchanged TradeTGD orchestration with frozen model responses and inert runner receipts; not a full worker/container pipeline. |
| Locked data environment: `tests/marketdata tests/portable/test_engine.py`, excluding live-provider tests | 216 passed, 3 skipped, 1 deselected | Installed SDK parsers, canonical calendars and fake-broker controller invariants; no live provider or native engine call. |
| Frontend Vitest | 368 passed in 21 files | Includes cached-result, error/retry and exact current-attempt contracts; not browser execution. |
| PostgreSQL/system collection | 303 collected | Collection only; no local database/process integration pass claimed. |
| Playwright collection | 28 collected in 6 files | Collection only; hosted browser gate remains required. |

TypeScript checking, production frontend build, full frontend formatting,
generated OpenAPI/client regeneration, scoped Python lint and diff whitespace
checks passed. Existing calendar/SDK and Starlette deprecation warnings remain;
the build also warns about large chart/editor chunks. These are not hidden as
successful performance or capacity measurements.

A separate scoped verification found no remaining actionable regression in the
exact admission-receipt binding, immutable package replay/download gates, and
portable current-attempt cache fence. It independently passed 363 targeted
backend cases and the 368 frontend cases. Actual-byte tests reject stale or
misbound final receipts and failed exposure commits before releasing a package;
valid replay/download preserves the archive's exact bytes. These fake-persistence
checks complement, rather than replace, the prepared real PostgreSQL cases.
