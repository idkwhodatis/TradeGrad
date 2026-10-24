# V1 strategy understanding and validation increment

This is the separately authorized increment dated 2026-10-23, not V2 or a new
claim that original V1 public-provider certification passed. The original
cumulative deterministic acceptance was GitHub Actions run `39942518018` at
published `42f6cc25f2e4525dcbaad4e58ee95cf733e286cd`. The later live-source gate
failed. SDK diagnostic `39956870702` established Yahoo resolution only, with
AKShare `ConnectionError` and OpenBB `EmptyDataError` (underlying causes unknown).
No live import/snapshot/seed result, paid-model call, broker or deployment is
asserted by this work.

## Reuse and phased acceptance

| Phase | New acceptance boundary | Reused foundation |
| --- | --- | --- |
| I0 | Leaf binding, typed missing states/source anchors, two executable controlled templates and manually derivable frozen fixtures | Evaluation, ExecutionAttempt, immutable candidate code, ResultBundle |
| I1 | Real runtime observations, copied pre-state, immutable partitioned Parquet, bounded owner-scoped reads, trace on/off parity | Native/portable isolated runners, artifact publication and resource fencing |
| I2 | Five-tab workbench, exact source/spec revisions, read-only controlled rule graph and lineage | Existing auth, router, Monaco, tables and charts |
| I3 | Decision/event/stage/instrument deep links, distinct non-trade reasons and execution links | Immutable datasets and result loaders |
| I4 | Frozen two-sided comparison, config differences, source-event pairing, both pre-states and explicit unchanged behavior | Existing config/result comparisons and code diff |
| I5 | Independent diagnostic replay from original start, semantic parity, two/three-group protocols and inherited exposure | Existing queue, lease, cancellation/recovery, experiments/splits |
| I6 | Versioned notes, typed evidence, search, Markdown export, retained/broken references | Owner-scoped persistence, artifact integrity and conservative cold cleanup |
| I7 | Real browser whole-flow acceptance, cross-owner/tamper/legacy/recovery checks and representative performance | Full PostgreSQL/Docker/Playwright/Compose CI |

A phase is recorded as complete only after its own checks. Local pure/unit,
contract and build results do not substitute for the final exact-head hosted
runtime gate. The original branch and its immutable evidence remain intact.

## Frozen I0 semantic choices

- Trace identity is physical `Evaluation.id`, its real `ExecutionAttempt.id`,
  exact result Artifact SHA-256, candidate code hash, dataset/universe versions,
  execution configuration and engine dependency digests. The native runner's
  `attempt_id` parameter is an evaluation sandbox identity, not the DB attempt.
- Optimized candidate code is not the run's seed StrategyVersion code. Both seed
  version and actual candidate/hash are retained; source/spec anchors use actual
  executed code. Old schema-less seed runs have no historical leaf Evaluation;
  no retrospective identity or false/zero/empty observations are invented.
- A cross-sectional decision is one complete portfolio event. Event pairing uses
  common source data event, callback phase, scope and event sequence. UUIDs,
  dates alone and per-stock joins are not event identity.
- Predicate states: true, false, not_evaluated, unknown, not_sampled. Feature
  validity: valid, warmup_insufficient, input_missing, calculation_error,
  not_collected, not_sampled. Absent pre-state is null with a reason, never zero.
- Descriptive StrategySpec is not executed. Controlled IDs map across supported
  parameter changes; arbitrary source mappings remain inferred/unmapped.
- Trace only copies values already computed. Filters affect saved detail, never
  the actual candidate pool. Limits and failures disclose partial coverage.
- Native strategy and engine share a process: observations are not an
  independently trusted ledger/no-lookahead proof. Portable internal logic is
  unavailable unless its actual protocol exposes it. The current portable
  listing cap remains 100; no silent ABI/capacity expansion is intended.
- Diagnostic state reconstruction starts at the source's original start plus
  warmup. Optimizer checkpoints are not complete engine checkpoints. The user
  observation window never replaces the actual reconstruction window.
- Original optimizer zero-closed-trade reward eligibility remains distinct from
  diagnostic/controlled valid non-trading results. No failure becomes zero profit.
- Exposure remains owner-wide and includes family/split lineage. Diagnostics,
  controls, trace reads and note evidence cannot reset final-holdout history.

## Representative measurement protocol, frozen before measurement

The representative **native trace-storage/query/comparison** workload is 1,000
listings × 750 daily sessions, baseline plus two variants, with the full candidate
pool retained in calculations. It does not certify the 100-listing portable ABI.
A separately labelled small real-AKQuant end-to-end fixture verifies runtime
integration. Synthetic trace-shaped data is not called a live market evaluation.

Reference target: Linux x86-64, Python 3.12, 2 dedicated logical CPU equivalents,
7 GiB memory budget, local SSD, PostgreSQL 18.6; record actual CPU, memory,
runtime/package versions and filesystem for every measurement. A different
observed hosted machine is reported, never silently called the target hardware.
The initial indexed single-session candidate page (50 rows, offset bounded) and
single-instrument/stage detail target is p95 ≤ 2,000 ms. At least 30 independent
query samples per shape, both first-open (cold application handle, OS cache not
claimed flushed) and repeated hot reads; report p50/p95/max and sample count.
Report actual rows seen/saved, collection elapsed time, peak RSS, compressed
artifact bytes, first comparison time and cached comparison time. Row/byte
truncation is a failure of the full-scale gate, not a speed optimization.
The first measured baseline will freeze regression budgets; no manufactured
measurement or final SLO is inferred from this initial engineering target.

## Scope restrictions

No arbitrary Python semantic reconstruction, visual strategy programming,
automatic causal proof, model-generated execution facts, vector store, graph
service, broker trading, paid-model calls, merge or deployment. New public data
is never downloaded as a side effect of opening a research page or diagnostic.

## I0 checkpoint

Validated 2026-10-23 before I1 work: 55 tests passed in 14.06 seconds, including both controlled
sources executed by actual pinned AKQuant 0.3.27. Required engine tests compared
trace-on versus trace-off orders, fills, positions, trades, equity and metrics;
future-input changes did not alter earlier observations; narrowed collection did
not shrink the actual strategy universe. Frozen expectations cover B selected,
higher-score C liquidity-filtered, rank losers, retained holdings, warmup,
missed timing, risk and cash constraints, deterministic ties and source anchors.
The observer/contracts also received independent read-only review. Ruff and
`git diff --check` passed. These are local engine/contract checks, not evidence of
PostgreSQL, Docker isolation, HTTP or browser execution for this increment.

Reproduce with an explicitly selected installed native test interpreter:
`TRADEGRAD_NATIVE_TEST_REQUIRED=1 TRADEGRAD_NATIVE_TEST_PYTHON=/path/to/python python -m pytest tests/research_trace -q`.
The required selector fails rather than silently skipping unavailable engine
checks. No real provider, model, broker, deployment or external call was needed.

The first aggregate run found only nonsemantic pandas row-index label differences.
The parity serializer now removes those labels while preserving every row value
and row order; the 55-test aggregate above is the subsequent successful rerun.

## Storage dependency and security boundary

The increment pins DuckDB 1.4.5 LTS rather than the initial 1.4.4 development
candidate. The [official patch release](https://github.com/duckdb/duckdb/releases/tag/v1.4.5)
includes backported out-of-bounds fixes. Existing runtime package versions remain
constrained; only DuckDB is newly added to the API and dependent worker locks.
The [DuckDB security model](https://duckdb.org/security) treats SQL as code and
file structures as trusted input: this application accepts neither user SQL nor
candidate Parquet. Candidate JSON is bounded and validated, the trusted worker
writes Parquet, and reads require immutable manifest/registration/hash agreement.
Automatic extension installation/loading is disabled. This is not a claim that
DuckDB itself supplies a sandbox or that hostile arbitrary files are safe.

## I1 checkpoint

Validated locally on 2026-10-23: **1,920 tests passed** in 63.30 seconds across
research trace, backend, supervisor, operations, immutable window and portable
configuration suites. The engine selector was required and pointed at the pinned
AKQuant interpreter. This includes real native/portable trace off/on/scope parity,
actual native entrypoint and OS-pipe relay checks, genuine partial fills, copied
pre-state, no repeated predicate evaluation, immutable publication and tamper
rejection. Separately, the original economic-result protocol's 43 tests passed
under the native interpreter. Frontend: **501 tests passed**, generated client,
typecheck, production build and format checks passed. Ruff and diff checks passed.
The existing Starlette/httpx deprecation and large lazy vendor-chunk build warnings
remain warnings, not runtime certifications.

Default summary and explicit research/off are staged immutably without changing
historical omitted-field configuration hashes. Four worker paths publish typed
leaf-bound traces in their existing lifecycle/retention guards and fenced result
transaction. Trace transport is separate from the unchanged 8 MiB economic
result envelope: 48 MiB compressed, 16 MiB per decision and the declared bounded
uncompressed capacity. The trusted worker writes Parquet date-range row-group
blocks, not one file per bar. A valid 9 MiB observation survives publication;
7,000 alternating-month events remain complete in fewer than 30 files. Large
individual browser details return an explicit bounded-detail error, while their
immutable artifacts remain available for authorized download.

Owner-scoped APIs and generic downloads enforce the original native/portable
research/final result admission, exact attempt/result/trace pins, source/config
bindings and exposure-before-commit. Candidate filtering includes eligibility,
reason, rank and finite score bounds. Historical absence is null with a reason.
Generic native execution snapshots retain observed order IDs but do not invent
market callback identities; intermediate native order transitions are explicitly
uncollected. Portable internal Python predicates remain unsupported unless its
actual protocol exposes them; its 100-listing ABI cap is unchanged.

Independent boundary review found and then cleared conflict tombstones, missing
rejection reasons, scope enforcement, valid oversized rows, out-of-order date
partitioning and integer/seen-counter limits. The workflow now requires the
capture engine suite and fail-closed PostgreSQL trace selector on the new feature
branch. The 16 new PostgreSQL HTTP cases are **not locally executed**; actual
PostgreSQL, Docker isolation, recovery and browser acceptance remain required at
the final exact-head hosted gate. Local HTTP-stub projection tests are explicitly
labelled and are not substituted for that gate.

## I2 implementation boundaries

- Descriptions are append-only, exact-code StrategySpec revisions for either a
  StrategyVersion or an actual Candidate. A prose change never changes code,
  execution parameters, graph edges, source anchors or historical observations.
  Reads of an explicit revision require its paired hash. The initial syntax-only
  description may be materialized under a source-row lock; it never evaluates,
  imports or re-runs strategy code and makes no model call.
- Only exact maintained native template bytes in the native execution mode have
  controlled graphs. Arbitrary or edited code receives inferred/unmapped source
  cards and explicit missing semantics. Identical template text in portable mode
  does not certify a native graph. Human prose is labelled `human_explanation`,
  not strategy-emitted or engine-observed evidence.
- Candidate code ancestry and actual algorithm references are separate. New
  optimizer checkpoints preserve the sampled candidate keys, actual revision
  mode, feedback hash and bounded UTF-8 preview, relevant logical model calls,
  and explicit unchanged-fallback/repair state at generation time. Old checkpoint
  candidates omit this optional context and remain unknown; no later sample set
  or code-parent relationship is relabelled as historical algorithm references.
  Existing random decisions, source prompts and logical-call replay are unchanged.
- Feedback previews are at most 64 KiB with an explicit truncation flag and hash
  of the complete feedback. Source model-call evidence remains independently
  addressable. The logical-call list is bounded by the existing 10,000-call run
  budget, not a smaller observer limit that could interrupt valid memory updates.
- The five-tab shell preserves exact source/spec and leaf attempt/result/trace
  pins. Later-phase tabs remain explicitly unavailable until their own increment;
  no static chart or placeholder is presented as completed replay/verification.
  This intermediate local checkpoint is not published as final acceptance.
- Leaf source associations preserve raw nullable candidate foreign keys and add
  separately verified provenance. Candidate association is either the exact FK
  and code hash, or the exact same-run candidate key and hash. Saved-source
  association requires a genuine seed identity; a generated unchanged-code
  candidate is not silently relabelled as the seed. A final leaf must match the
  immutable owned FinalSelection's selected version, source candidate and hash.
  Both paginated discovery and direct deep-link admission use the same predicates.

## I2 checkpoint

Validated locally on 2026-10-23 before I3: **1,952 tests passed** in 58.91 seconds
across the research-trace, API/backend, runner, operations and configuration
aggregate, with actual native capture execution required. A separate current
hash-locked research runtime passed **118 optimizer/provenance/model-boundary/
worker tests** in 11.56 seconds, including actual source sampling/prompt parity
and crash/resume parity. Its 56 exact package versions and the API's 50 exact
versions passed runtime-separation checks. Frontend: **569 tests passed** (68 new),
typecheck, production build and formatting passed. The new lazy workbench chunk
is about 210 KiB (66.75 KiB gzip); existing large Monaco/ECharts vendor chunks and
the existing Starlette/httpx deprecation remain warnings. Ruff checked all 25
changed Python files; diff checks and the preserved original checkout are clean.

Independent review found and cleared controlled-template mode handling, separate
native/portable final leaf discovery, source-only audit writes, immutable-context
downgrade protection and null-FK/same-code source-association edge cases. Source
and leaf verification now precede trace fetching and rendering. Migration 007
adds append-only spec revisions, separate bookmarks and optional immutable
candidate generation context. The cold PostgreSQL restore drill now includes
007 descriptions, bookmarks and context; historical 003 data stays unmodified.

**25 new PostgreSQL cases are collected, not locally executed.** The existing
native and portable Docker final pipelines now also assert workbench discovery,
selection and foreign-owner denial. **54 Playwright scenarios are collected**
in 13 files, including seven new real-stack I2 scenarios for both templates,
read-only graph/source navigation, lost-response idempotent prose editing,
history/login restoration, ownership and real isolated leaf/bookmark workflow.
The baseline result-workbench browser suite is unchanged. Collection and local
projections are not browser, PostgreSQL, Docker or recovery certification. The
hosted gate requires the workbench PostgreSQL selector and remains mandatory on
the exact final published head.

## I3 implementation boundaries

- Replay keeps the physical leaf, attempt, immutable result and trace hashes in
  the URL alongside the specific decision event, session, instrument and stage.
  A date never collapses distinct callbacks or substitutes for a decision ID.
  Source association must be verified before trace requests or rendering.
- Exact selected observations preserve their saved per-stage/table ordinal as
  well as decision, instrument and logical ID. Duplicate feature/condition or
  candidate IDs never silently resolve to the first row. A registered off-mode
  manifest retains its actual hash and missing state; it is not historical
  absence and is not a stale-response error.
- Three bounded replay projections reuse the same four schema-specific result
  loaders, final-result admission, immutable storage and exposure release. They
  read frozen OHLCV and effective configured universe files, not providers or
  current listing catalogs. Retrospective price charts are not no-lookahead
  evidence; user-fixed/current universes are not historical membership proof.
- Execution evidence joins only explicit saved logical/child-order/fill IDs into
  the admitted economic bundle. Symbol/time proximity never establishes an
  association. Generic native execution snapshots remain explicitly unpaired.
  The original decision pre-state remains separate from later execution events.
- Capture authoring exposes off/summary/research, observation windows, filters
  and budgets. These narrow retained observations, never the strategy candidate
  pool or actual backtest window. Historical omitted capture fields retain their
  original canonical configuration identity.
- Five new actual-AKQuant-to-DuckDB/Parquet checks cover higher-score filtered
  candidates, rank losers, holding retention, configured-universe exit, warmup,
  false versus not-evaluated predicates, summary missing detail, same-day distinct
  decision/execution events and actual multiple/partial fills. Already-computed
  valid scores can remain present for an outside-universe candidate; membership
  is not a feature-validity predicate. Synthetic business binding UUIDs/hashes in
  these query fixtures are labelled, not represented as PostgreSQL/HTTP admission.

## I3 checkpoint

Validated locally on 2026-10-23 before I4: **1,997 tests passed** in 75.95 seconds
across research trace, API/backend, runner, operations and configuration suites,
with actual native capture execution required. The five new end-to-end native
query checks ran AKQuant 0.3.27 and read the resulting Parquet through DuckDB;
they are not substitutes for database, HTTP or container admission. Ruff and
format checks passed for all 15 changed Python files; diff checks passed. The
existing Starlette/httpx deprecation remains a warning.

Frontend: **701 tests passed**, typecheck, production build and formatting
passed. The workbench chunk is 245.80 KiB (75.57 KiB gzip), with a separately lazy
37.61 KiB replay chart (12.93 KiB gzip); existing large vendor chunks remain a
warning. Independent review cleared duplicate-observation ambiguity, selection
restoration after pagination/filter changes, registered capture-off missing
states, dependent-selection clearing and UTC chart rendering. The baseline
result-workbench browser suite remains unchanged.

**42 PostgreSQL tests/selectors and seven selected system cases were collected,
not locally executed.** The replay PostgreSQL group includes one required-gate
selector and 41 actual cases. Four native/portable research/final system paths
now inspect replay admission and immutable evidence. **57 Playwright scenarios
were collected** in 14 files, including three new real-stack replay and capture
cases. These use actual isolated evaluations without browser route stubs; the
cross-sectional browser case uses its declared two-listing normalized fixture,
not the four-symbol hand-worked truth fixture. The latter is separately covered
by the actual native-to-Parquet query tests. Collection does not certify
PostgreSQL, Docker, Chromium or recovery. The hosted gate requires the replay
PostgreSQL selector and final exact-head acceptance remains mandatory.

## I4 implementation boundaries

- Comparison is a derived asynchronous ResearchRun with its own execution
  attempt, immutable ordered source pins and publication manifest. It is not
  another Evaluation and never runs candidate code, a model or a provider.
  Input-hash reuse is owner-scoped; additional submission keys are durably
  reserved and cannot be reused by another run type.
- Both physical leaves, source code/spec revisions, result/trace manifests and
  final-admission fences are independently rechecked. Result reads, comparison
  pages and downloads record both source exposures before response release.
  Generic SSE carries bounded status and artifact readiness only, not outcomes.
- Source-event pairing requires a verified common data, universe and instrument
  namespace, then uses data-event identity, callback phase, decision scope and
  event sequence. Random record IDs, date/symbol proximity and generic native
  order IDs are not event identity. Added, removed and ambiguous events remain
  visible; both original pre-states are retained without a causal attribution.
- Exact missing states and observation scope survive comparison. Narrow capture
  does not certify whole-universe equality, absent optional tables are unknown,
  and duplicate observation IDs retain original ordinals and multiplicity.
  Reported unchanged behavior names the actual observed sections and window;
  it is neither diagnostic parity nor an intermediate-state counterfactual.
- Comparison DuckDB runs synchronously under an attempt-directory flock and
  storage lifecycle barrier. Repeated cancellation/deadline interrupts do not
  themselves prove termination. Recovery must obtain the same durable inode
  lock, and publication waits for actual query closure. A Docker stop is never
  used as proof that an in-process comparison has stopped.
- Fixed limits bound aggregate input/output, events, per-event rows and bytes,
  files, memory, runtime and browser response sizes. Capacity failure is visible,
  not hidden truncation or a smaller calculation universe. Browser reads remain
  server-paged; immutable comparison files retain exact source references.

## I4 checkpoint

Validated locally on 2026-10-23 before I5: **2,103 tests passed** in 88.00
seconds across backend, runner, operations, trace and configuration suites with
actual native execution required. Final comparator safety refinements were
additionally checked in **65 comparison/scale-smoke/actual-native tests** in
14.38 seconds and **12 worker closure/fencing seam tests** in 0.71 seconds. The
separate unchanged-native large result protocol passed **43 tests** in 7.39
seconds, and the hash-locked research runtime passed **167 optimizer/provenance/
model-boundary/worker tests** in 19.32 seconds. These focused groups overlap the
broad suite where applicable and are not an additive unique-test count.

The four new actual AKQuant comparison truth tests execute distinct baseline,
repeat, liquidity-change, comment-only and narrow-capture runs, publish their
real trace through Parquet, and compare event identities and actual economic
observations. Synthetic business UUIDs are explicit and do not claim database
admission. The 1,000-listing trace-shaped test ran only a **three-session smoke**;
its reusable 750-session baseline-plus-two-variant generator has not yet been
measured. There is no representative latency, memory or SLO claim at I4.

Frontend: **812 tests passed** in 53 files, with typecheck, production build and
formatting passed. The workbench chunk is 286.71 kB (86.43 kB gzip), with a
separate lazy comparison chart; existing Monaco/ECharts vendor-size warnings
remain. All 143 pre-existing API paths remain unchanged; the 242 previous schema
contracts are unchanged except additive backup receipt revision support for
007/008. Seven comparison paths and 20 component schemas were added. Ruff and
format checks passed for 36 changed Python files, and whitespace checks passed.

Independent review cleared query-close cancellation, uncertain-close guard
retention, shutdown during retry, bilateral final admission, missing target
coverage, capture-scope equality, mechanism parameter granularity, duplicate
observations, bounded non-greedy tolerance matching, explicit execution graph
topology and exact replay-link admission. Matching work has fixed declared node,
byte and cardinality budgets; unresolved matching remains ambiguous rather than
inventing equality or a difference. POSIX tests use actual owned child processes
and kernel locks; mocked persistence seams are labelled separately.

**56 comparison PostgreSQL cases/selectors were collected, not locally run**
(54 database cases, one required-environment selector and one pure portable
fixture contract). They cover genuine native/portable final loaders on both
sides, cache/idempotency races, cancellation/publication ordering, rollback,
lease expiry, ownership, retry, append-only bindings and process-death recovery.
The separate seven-case backup/upgrade group includes real 003-to-008 migration
and 008 cold restore gates, likewise collected only. The source results in these
DB boundary fixtures are explicitly synthetic, not engine certification.

**59 Playwright scenarios were collected** in 15 files, including two new real
stack I4 scenarios for comment-only observed equality and mechanism/config
changes with both pre-states. They require actual isolated evaluations, exact
source-kind/history restoration, immutable result hashes, server paging,
mandatory saved order-association drilldown, login restoration and cross-owner
denials. No route stubs or fabricated trace fallback are used. PostgreSQL,
Docker, Chromium and recovery acceptance remain pending the mandatory exact-head
hosted gate; the original baseline browser suite is unchanged.

## I5 implementation boundaries

- Diagnostics and each manual control arm are dedicated one-shot native or
  portable research runs, each with a new physical attempt and Evaluation.
  They do not imitate an optimizer with zero iterations, call models, download
  data, or reinterpret an optimizer checkpoint as a complete engine state.
- Immutable original code, specification, result/trace manifests, data/universe,
  original execution conditions, runtime images and observed RNG are pinned.
  Reconstruction always starts at the original full scored window plus warmup;
  the requested observation window only limits retained detail. Unsupported
  historical sources expose the reason rather than substituting current inputs.
- Native observation captures Python and NumPy-legacy RNG immediately before
  source import. Portable policy captures Python RNG at its pre-import seam;
  the separate engine provides audit receipts. Original-start replay restores
  the same state at the same seam. OS entropy, default_rng, self-reseeding and
  arbitrary unreviewed source remain outside proven randomness coverage.
- Actual AKQuant public audit events preserve ordered order transitions, explicit
  fill parents and nanosecond times. Sidecars have separate bounded transports;
  missing, dropped, errored, malformed or incomplete receipts never establish
  parity. Native shared-process observations are not an independent ledger or
  no-lookahead proof. No strategy predicate is reevaluated for explanation.
- The immutable parity report has 14 mandatory named checks: source, fixed
  conditions, RNG reconstruction, randomness coverage, audit completeness,
  submitted intents, ordered statuses, fills, position/account trajectories,
  metrics, original-event mapping, both pre-states and actual targets/plans.
  Unknown values never become empty targets or zero state. Cross-run order IDs
  are removed only after resolving explicit local graph edges. Open-order
  references additionally anchor the full ID-free observed child graph, not
  only a potentially nonunique final order row; these anchors are exact while
  economic fields retain the declared fixed tolerance table.
- Only a completely matched report marks a new, distinct decision as a verified
  original-event mapping. Otherwise its own observed evidence is retained with
  mismatch/not-verifiable status. The report is typed and artifact/hash bound;
  even generic downloads repeat owner, source and final-admission validation.
  Native execution-config hashes are distinct from independent wrapper hashes.
- A trusted independent result policy preserves legitimate successful no-trade
  and no-closed-trade native outcomes, with unavailable metrics null and optimizer
  reward explicitly not applicable. Exceptions remain failures. Existing native
  optimizer reward/positive-trade admission and legacy artifact schemas remain
  strict and separate. Native metrics retain their own units and definitions.
- Manual experiments have two or three immutable ordered arms, including the
  exact baseline. The common protocol excludes only the deliberate code and
  parameter values, not declaration/state/warmup/data/execution conditions.
  Their child split preserves the original research family and holdout exposure.
  Exploration controls cannot use the final holdout. Diagnostics of admitted
  finals require their own exact new-generation final admission before reading.
- Admission and retries revalidate frozen input bytes before queue dispatch;
  cancellation/recovery require both the actual host-query inode lock and
  confirmed Docker stop. A timed-out lease is not proof of stopped execution.
  Pause is unsupported explicitly. Independent source chains are prospectively
  capped at 16 and checked with owner/session-scoped pin memoization.

## I5 checkpoint

Local I5 review and checks were performed on 2026-10-23 before I6. The final
broad API/runner/operations/trace selection passed **2,203 tests** in 105.93
seconds; all actual engine checks require the pinned AKQuant interpreter and
cannot silently opt out. The separate original native large-result protocol passed **43 tests** in 7.29
seconds in the native interpreter, not the dependency-minimal API environment.

The final focused diagnostic/portable-real-engine/storage/recovery/CI-barrier
selection passed **87 tests** in 8.42 seconds. The native pair really executes
both exact maintained-source runs and restores original RNG; all 14 checks
match and eight genuine portfolio decisions, including a non-trade day, map to
new IDs. Five real portable-engine scenarios independently test full/partial
fills, rejection, cancellation and no-trade audit/result edges and account
coverage; they are not represented as complete portable replay certification.
Negative cases cover absent targets/pre-states/audits, incorrect RNG restore,
wrong source events, bogus fill IDs, exact nanosecond mismatch, explicit logical
parents, selected-stage gaps and ambiguous final-row pre-state references.

The separate locked research-runtime optimizer/provenance/worker selection
passed **357 tests** in 17.70 seconds. Runtime-owned validation included **104
focused passes**, **148 legacy-native passes**, and **988 portable passes with
one existing calendar skip**; focused build/publication closure checks passed
again after final additions. These overlap broader selections and are not
additive unique-test totals. Backend-specific final checks passed **412 tests**
with 39 explicitly unexecuted infrastructure cases. All reported P1/P2 review
findings were fixed and independently rechecked; no unresolved concrete finding
remained, conditional on the hosted gates.

Frontend: **932 tests passed** in 58 files, TypeScript, production build,
formatting and whitespace checks passed. **61 Playwright scenarios were
collected** across 16 files, including two independent diagnostic/control cases;
collection is not browser execution. Generated OpenAPI/SDK includes the admitted
physical execution hash and exact native metrics. Source/spec/code/history
selections remain hash-bound when returning from new diagnostic observations.

**42 independent PostgreSQL/backup cases and four system scenarios were
collected, not run locally.** Required CI now executes the original-start native
and portable diagnostic pipelines, real native sandbox cancellation/retry,
three-arm no-trade/failure retention and unchanged optimizer rejection, and an
exact-SHA maintained native template requiring all 14 parity checks. All three
images are rebuilt from pinned closures; each isolated group must prove fixture
teardown before the next starts, and prior failures are not suppressed.
Migration 009 has offline PostgreSQL SQL rendering and a real hosted upgrade/
restore gate. Its restore fixture covers a cancelled independent persistence
graph and restored immutability triggers, not successful parity artifact reopen.
Completed-result reopen remains an I7 integrated recovery acceptance boundary.

Local socket/PostgreSQL/Docker restrictions still prevent the real application
stack. Chromium's executable is present, but this does not make an actual-stack
browser run possible by itself. Final exact-head PostgreSQL, Docker, Playwright,
Compose and recovery checks remain mandatory; no fixture is called live evidence.

## I6 implementation boundaries

- Research notes are human-maintained hypotheses and conclusions, not executable
  strategies or automatically validated profit claims. Structured fields cover
  motivation, rules, sources, observed behavior, controls, counterexamples,
  applicability and next steps. The four explicit statuses are pending,
  supported-under-current-tests, mixed and refuted-under-current-tests.
- Every saved revision is append-only and binds its contents, evidence snapshot,
  physical retention inventory and inherited admission dependencies to an exact
  hash. Owner-scoped idempotency and optimistic base revision/hash checks prevent
  retry duplication and silently overwriting a concurrent edit. Removing a
  visible reference never removes earlier outcome-access dependencies.
- Typed references preserve code and specification revisions, physical leaf
  Evaluation/Attempt/result/trace pins, decision/stage/instrument selectors,
  comparison sides and event pairs, parity reports, and all immutable control
  arms. A failed arm remains a failed physical generation, never zero profit or
  a later successful retry. Rule-only notes do not claim execution evidence.
- Reads, history, search, exports and idempotent responses enforce owner and
  final-result admission before returning saved outcome prose. Search admits the
  bounded owner corpus before filtering/counting, so empty pages and counts do
  not become an outcome oracle. Evidence metadata is admitted before fallible
  byte loading; a missing file cannot hide another side's admission requirement.
- Exact missing, unavailable and corrupt states are visible without replacement
  by current code or newer executions. Restore selections must match one
  coherent saved source/evidence tuple; individually valid fields cannot be
  mixed across two executions. Markdown export renders user text literally and
  includes the exact evidence inventory, rather than active user-supplied HTML.
- Historical control-arm locators bind the note revision/hash and evidence/arm
  ordinal. Their read-only panel displays the saved physical status, generation
  and stopped attempt even after a retry, without consulting current run status.
  Successful arms can open their exact result; a failed arm has no result link.
  Generic current-control links are suppressed when they cannot restore history.
- Note resolution has bounded content, reference counts, ancestry, file bytes,
  file counts and elapsed work. Search/history stream one revision at a time,
  with a shared 32 MiB revision-read budget and a 1,000-note search-corpus cap.
  The 30-second work deadline is cooperative around individually bounded reads,
  not a claim to interrupt a single operating-system read precisely at 30 seconds.
  It never runs a strategy, model or provider.
  Source/session/owner pin memoization remains request-local. Exceeding a bound
  is an explicit error, not silent truncation of the evidence corpus.
- Cold backup and cleanup inventory includes artifact pins from every historical
  revision independently of live artifact registration. Exact duplicate pins
  coalesce; conflicting hashes and absent/corrupt retained bytes fail closed.
  Existing cleanup remains conservative: only expired unreferenced atomic
  temporary files are candidates. No automatic published-evidence deletion or
  vector database is introduced.

The migration-010 recovery fixture creates and reopens two real source-note
revisions through the service, plus a separately labelled synthetic persistence
fixture for a registered artifact retention pin. This verifies persistence and
inventory contracts, not a successful strategy evaluation. Completed diagnostic
and control result recovery is still a separate I7 acceptance requirement.

## I6 checkpoint

Validated locally on 2026-10-23 before I7: **2,282 tests passed** in 109.27 seconds
across backend, runner, operations and research-trace suites. The actual native
engine selector remained required and used the pinned AKQuant interpreter.
The final note contract selection independently passed **72 tests**. Focused
backup/retention/CI-boundary checks passed **324 tests**; these overlap the broad
suite and are not additive totals. Generated OpenAPI/SDK, Ruff, formatting,
whitespace checks and offline PostgreSQL migration rendering passed.

Frontend: **1,000 tests passed** in 63 files; TypeScript, production build and
format checks passed. **63 real-stack Playwright scenarios were collected** in
17 files, not locally executed. New scenarios cover exact note restoration after
specification edits and login, concurrent revision conflicts, owner checks,
escaped exports, and historical failed-arm generation inspection after retry.

The note suite contains **36 PostgreSQL-selector/API cases**: its selector check
runs locally and 35 database cases remain unexecuted. The combined note/backup/
OpenAPI local selection passed 15 checks with **42 explicit infrastructure
skips**. No SQLite substitute or successful recovery claim is made. Required CI
enables the note PostgreSQL selector and keeps the full existing browser suite.

Independent review found and then cleared inherited outcome-admission laundering,
search/history count and empty-page leaks, missing protected authority, mixed
source restore selections, stale cached frontend outcome display, unsafe export
markup and eager large-corpus materialization. No concrete blocking finding
remained in the reviewed boundaries. Exact-head hosted PostgreSQL concurrency,
cold restore, Docker and actual browser validation are still mandatory in I7.
