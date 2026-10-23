# Original TradeGrad WebUI V1 acceptance audit

Audit date: 2026-10-23 UTC. Repository: `idkwhodatis/TradeGrad`, V1 feature branch.

Audited source: M6 preparation `c0a0d7102c979d78afb0f9fe14b65e00fac3aaab` plus the model core subsequently sealed as `5b3f0ee24fbce432594a2df1c3f6cb3fa7cc5e2f`. Line numbers describe that audited source, not later edits. This is a static acceptance-coverage audit, not a security re-review or a fresh test execution. No service, socket, provider request, strategy, model, test suite, commit, publication, or credential operation was performed.

Plan: the supplied `TradeGrad WebUI V1 与实盘框架适配 V2 实施计划.md`. All “L” references below are exact lines in that supplied file. Sections 11–13 and explicit V2-only rows are excluded. Section 14 is included where it applies to V1.

## Conclusion

An unqualified “the original V1 is complete” is not supported, even if the pending M5/M6 hosted gates eventually pass. There are explicit, functional scope reductions, not merely missing external verification: no benchmark configuration/execution; no planned Settings management; no full application Compose deployment; no strategy-declared native data/parameter contract; several specified chart interactions absent; no configurable retained-log/temp cleanup or full operations measurements; and no explicit Yahoo repair-derived import. Dataset provenance also lacks the requested terms URL. These need implementation or an explicit scope decision, not promotion of the existing tests into broader coverage.

Portable is materially constrained to synthetic data and frozen model replies. However, the plan does **not** explicitly require a named real-provider portable acceptance run in its narrow M5 gate. The two templates, separate trusted ledger, decisions, final tests, and export can meet that gate using deterministic fixtures. The broader product promises two usable modes and LLM optimization, so the restriction must remain prominent, and portable real-data/model enablement must not be silently declared delivered. See the precise distinction below.

## Evidence interpretation

- **tested**: implemented, with recorded successful hosted evidence for the relevant M0–M4 path. This does not imply all later changes passed on this working tree.
- **implemented**: concrete implementation and tests exist, but the relevant exact-head hosted acceptance is pending/unverified here, or only a bounded subset is covered.
- **deferred**: the required behavior is absent, rejected by the contract, or replaced by a narrower behavior. An honest warning does not implement the missing feature.
- **external-blocked**: a specific external prerequisite remains unresolved. It is not used as a label for ordinary code backlog.
- A row can have multiple statuses when a single original line contains both delivered and missing clauses.
- “Tests” identifies concrete evidence sources, not an assertion that this audit executed them. Release accounting must attach the latest exact-head outcome for M5/M6/model overlay; a running, collected, skipped, or synthetic-only gate is not a broader pass.

Recorded hosted baseline, corroborated in repository evidence:

| Evidence key | Exact source / run | Scope |
| --- | --- | --- |
| H01 | `f66736a1da516f6a2650318bfa4c72992ad37951`, run `39801546496`; `docs/M1_VALIDATION.md`, `docs/V1_IMPLEMENTATION.md` | M0/M1 actual four-task engine, PostgreSQL/queue, Docker isolation, worker recovery, Chromium |
| H2 | `0f6e83599ce3251dcdda1a97345d314271c53f3c`, run `39868584582`; `docs/M2_VALIDATION.md` | Three real adapters through frozen SDK responses to immutable snapshots and original seed evaluation; **not live upstream acceptance** |
| H3 | `5f9125caa47107824f75094dcec08b799a829b8f`, run `39870319437`; `docs/M3_VALIDATION.md` final section | Actual source TradeTGD, corrected warm-up, frozen-model optimization, durable recovery and browser |
| H4 | `4dba3edc515a99345de806679f64091faddb8dd1`, run `39875412249`; `docs/M4_VALIDATION.md` | Configured browser research, persistence/results/comparison/ownership |
| P5 | M5 repaired head `7adc216f467f10ef04a069f4fdfcc21ae2b353c7`, [run 39879090068](https://github.com/idkwhodatis/TradeGrad/actions/runs/39879090068) | PostgreSQL passed 294 tests; paired-container proof failed 31 cases and passed 20. Later application/browser gates did not run. Narrow diagnostic-only repair is in progress; M5 is not accepted. |
| P6 | Local M6 `427e0f1522874fb970c213d3e4241fe1de048059`, then fixture repair `c0a0d71`; `docs/M6_VALIDATION.md` | Prepared restore/upgrade/performance and cumulative gate, not a fresh accepted release in this audit |
| PM | Model core `5b3f0ee`, isolated hosted-browser preparation `a4500bd`; `docs/V1_OPERATOR_MODELS.md` | Native-only owner-allowlisted profiles, default off. Available cumulative pure suites passed; exact-head synthetic SDK/browser acceptance pending. No real provider calls made. |

## Exact-plan-line matrix

Each normative V1 requirement line or inseparable line group is mapped below. Explanatory links, headings, diagrams and estimates do not create additional acceptance requirements. Repeated requirements retain their own plan line and cross-reference the same concrete implementation.

| ID | Plan line(s) | Requirement | Status | Concrete code / test evidence | Acceptance gap or interpretation |
| --- | --- | --- | --- | --- | --- |
| V01 | L3, L7 | Three adapters and browser source→snapshot→research→compare→saved strategy workflow | tested + implemented | `packages/marketdata/providers/{akshare,yfinance,openbb}.py`; `frontend/src/features/{data-sources,data-datasets,new-experiment,research-detail,compare}.tsx`; final-selection/package features; H2–H4, P5 | Provider E2Es stop at seed evaluation; optimization E2E uses a separate upload. Final selection/export requires P5. No complete live-provider claim. |
| V02 | L9 | CN/US daily, time/cross tasks; owner permissions | tested | `packages/tradegrad_adapter/config.py:25`; `tests/baseline`; `tests/integration/test_ownership.py`; H01/H3 | Four tasks covered. Multi-market portfolios, minute data and brokerage remain properly excluded. |
| V03 | L21, L29 | FastAPI template, login/user management/application skeleton | tested + deferred | `backend/app/main.py:246-284`; `backend/app/models.py:33-45`; `docs/FASTAPI_TEMPLATE_LICENSE.txt`; H01 | Login/owner auth exists. User lifecycle management is operator bootstrap only; no management UI/API. See V33. |
| V04 | L22 | PostgreSQL-backed Procrastinate | tested | `workers/research/{queue,dispatcher}.py`; `tests/integration/test_outbox.py`; H01/H3 | Implemented. No replacement queue or SQLite acceptance. |
| V05 | L23, L50, L189 | Retain TradeGrad algorithm with thin configurable AKQuant execution | tested | `packages/tradegrad_adapter/optimizer.py:104,346`; `workers/research/optimization.py:164`; `tests/tradegrad/test_optimizer_source_parity.py`; H3 | Actual source `TradeTGD`, not a replacement optimization loop. |
| V06 | L24 | Independent AKShare/yfinance/OpenBB adapters | tested | Independent files and SDK paths in `packages/marketdata/providers`; `tests/marketdata/test_sdk_paths.py`, `test_openbb.py`; H2 | Synthetic SDK responses do not establish upstream availability. |
| V07 | L25 | PostgreSQL metadata, Parquet and file artifacts | tested | `backend/app/models.py`; `packages/marketdata/storage.py:19`; `backend/app/storage.py`; H2 | Present. Immutable checksums and ownership retained. |
| V08 | L26 | Native and portable paths | tested + implemented | `backend/app/research_routes.py`; `backend/app/portable_routes.py`; P5 | Native usable path exists. Portable synthetic-only boundary is material; see V81–V89. |
| V09 | L37–41 | React/TS/Vite/shadcn, TanStack, RHF/Zod, generated OpenAPI client | tested | `frontend/package.json`; `frontend/src/client`; `frontend/src/lib/research-form.ts`; H4 | Client generation, forms and cache paths implemented. Full table sorting is separate V29. |
| V10 | L42–44, L53 | Price/trade/indicator, analysis and code-diff visualization | tested + deferred | `frontend/src/components/price-chart.tsx:11-49`; `research-chart.tsx`; `code-editor.tsx`; H4 | OHLCV, equity/drawdown, optimizer/candidate plots and Monaco exist. No trade-marker/indicator linking, monthly return heatmap or return distribution. See V37–V38. |
| V11 | L45–46 | FastAPI/Pydantic/SQLAlchemy/Alembic domain services and durable jobs | tested | `backend/app/models.py`, `backend/alembic/versions`; `backend/app/services.py:103,238,286`; H01–H4 | Implemented for owner-private resources. |
| V12 | L47 | Pandas/PyArrow/DuckDB processing, snapshots/query/CSV export | tested, implementation variant | `runtimes/data/requirements.in`; `packages/marketdata/{normalize,storage}.py`; bounded preview in `backend/app/data_routes.py` | Pandas/PyArrow/CSV are present; DuckDB is not. This is a component-choice deviation, not by itself an unmet user outcome if bounded-query capacity is validated. Do not add a dependency merely to claim use. |
| V13 | L48 | Versioned exchange calendars and market boundary tests | tested | `packages/marketdata/{calendars,venues}.py`; `tests/marketdata/test_core.py`, `test_portable_schedule.py`; H2 | Shenzhen/XNAS use explicitly labelled XSHG/XNYS proxies. Not silently authoritative venue equivalence. |
| V14 | L49 | Export live AKQuant result object to ResultBundle | tested | `packages/tradegrad_adapter/results.py:333-524`; `tests/tradegrad/test_results.py`, `test_results_entrypoint.py`; H3 | Structured core ledger/series persist; benchmark missing (V50). |
| V15 | L51, L61 | Independently locked API/data/research/evaluator runtimes and images | tested + deferred | `backend/requirements.lock`, `runtimes/{data,research,evaluator}/requirements.lock`; `script/validate_runtime_split.py`; evaluator/data Dockerfiles | Locks exist. API/research application image deployment is absent; no full Compose. See V18. |
| V16 | L57, L59, L286 | Bounded monorepo/service architecture; avoid additional platform sprawl | implemented | One backend; separate data/research workers; packages and runtime directories | Directory naming differs harmlessly (`marketdata` rather than `data_adapters`). No Refine/MLflow/second admin framework needed. |
| V17 | L82 | API control, worker coordination, isolated evaluator, authenticated HTTP/SSE | tested | `backend/app/main.py:476-535`; worker executors; `frontend/src/lib/api.ts`; H01/H3/H4 | Implemented. PM changes need their own exact-head gate. |
| V18 | L86 | Full single-host Docker Compose application+PG+data+research+runner deployment | deferred | `infra/compose.yml:1-21` contains database only; `docs/V1_OPERATIONS.md:74-108` explicitly starts seven host commands including Vite dev | Clear plan mismatch. A documented host development setup is not the promised full Compose install. |
| V19 | L88 | Nonprivileged read-only no-network evaluator; bounded resources; no secrets/socket | tested + implemented | `runtimes/evaluator/runner.py`, `runtimes/evaluator/portable_runner.py`; `tests/isolation/test_docker_evaluator.py`; `tests/portable/test_isolation.py`; H01 native, P5 portable | Candidate containers satisfy intended boundary; portable physical isolation still needs P5. Not an assertion of hostile multitenant certification. |
| V20 | L92 | Migrations, liveness/readiness, heartbeats, backlog, import failures/wait/runtime/tokens/cancel latency/disk and tagged logs | tested + deferred | `backend/app/main.py:187-244`; `research_usage` in `research_views.py`; run/attempt timestamps; `frontend/src/features/overview.tsx` | Readiness, heartbeats, timestamps and model usage exist. `/status` counts undelivered owner outbox records, not complete queue backlog. No persistent/visible import-failure rate, queue-wait/cancel-latency aggregate, disk usage baseline found. |
| V21 | L94 first sentence, L395 first clause | DB+snapshots+artifact backup and restore rehearsal | implemented | `backend/app/operations_backup.py:801,895,996`; `script/backup_v1.py`; `tests/integration/test_backup_restore.py:470`; `docs/M6_BACKUP_RESTORE.md` | Real cold empty-target restore/upgrade gate P6 must finish. File/fake-client checks alone do not establish restoration. |
| V22 | L94 second sentence | Retain final artifacts; configurable temporary/high-frequency-log cleanup without deleting referenced data | deferred (retention cleanup) | Immutable storage preserves artifacts; `backend/app/core/atomic_storage.py:159-204` only immediate temp cleanup; `docs/M6_FAILURE_MATRIX.md` records retained orphan files | No configured age/retention sweeper or safe reference-aware GC found. Do not confuse write-finally cleanup/container teardown with planned retention policy. |
| V23 | L96 | Ordered authenticated SSE resume and resource-scoped lists/charts | tested | `backend/app/main.py:476-535`; `tests/integration/test_ownership.py::test_sse_replay_has_ordered_ids_and_resumes_after_last_event`; H01/H4 | Implemented; no URL bearer token. |
| V24 | L102 | Overview active/recent/resources/data health/queue/failure/model usage | tested + deferred | `frontend/src/features/overview.tsx`; `/status`; H4 | Main overview present. Disk/resource and complete queue/operational measurements not delivered, see V20. |
| V25 | L103 | Data Sources manage adapters, test connection, capability versions/last verification | tested + deferred | `frontend/src/features/data-sources.tsx:46-132`; `backend/app/data_routes.py:69-93`; `packages/marketdata/catalog.py:13-68` | Inspect/resolve/import implemented. Catalog is static documented capability metadata; no connection CRUD or distinct persisted connection-test/last-verified workflow. Resolution is not a persisted full capability verification receipt. |
| V26 | L104 | Resolve/download/upload/preview/publish/refresh with visible coverage/semantics | tested | `data-{sources,imports,datasets,upload}.tsx`; `tests/integration/test_data_imports.py`; `frontend/tests/e2e/data-workbench.spec.ts`; H2 | Implemented for declared daily subset. Live-source evidence outstanding. |
| V27 | L105, L179 | Fixed/current/historical universes, validity/known times/PIT status | tested | `frontend/src/features/data-universes.tsx:26-180`; `packages/marketdata/contracts.py:296-317`; `backend/app/data_services.py:446`; `tests/integration/test_data_universes.py`; H2 | Manual current/historical member input works; no independently verified PIT source is claimed. Disabled manual PIT promotion is correct, not an omission requiring fabricated evidence. Provider universe methods are optional in L149. |
| V28 | L106, L239, L258 | Strategy upload/edit/diff; native/portable, parent, parameters and capability requirements | tested + deferred | Native `StrategyCreate` in `strategy_schemas.py:12-22`; `strategy_services.py:75-138`; `catalogs.tsx:35-49`; portable template creation `portable_schemas.py:64-70` | Native source CRUD/diff exists. Native schema has no parameter/data/capability contract; portable API creates fixed reference templates only, no general policy upload/edit UI. Saved native parameters are embedded in code, not a managed parameter contract. |
| V29 | L107, L115 | Wizard pins inputs/mode/windows/market/cost/model/budget and preflights | tested + implemented | `new-experiment.tsx`; `portable/experiment.tsx`; `config.py`; H4 native; P5 portable; PM native model picker | Core four-step native wizard implemented. Separate portable wizard is acceptable; unavailable benchmark and general strategy contracts are tracked separately. |
| V30 | L108 | Run detail phase/logs/pause/cancel/resume/lineage/window metrics/cost/checkpoint | tested + implemented | `research-detail.tsx`; `research_routes.py:134-170`; `research_services.py`; H3/H4; P5 portable | Token/call usage without fake currency cost is allowed by L222. Portable/final lifecycle evidence distinct. |
| V31 | L109 | Backtest equity/drawdown/benchmark/trades/positions/fees/data assumptions | tested + deferred | `backtest-detail.tsx:310-375`; `results.py:480-511`; H4 | Everything listed except usable benchmark input/series; no synthesized substitute. See V50. |
| V32 | L110 | Compare same-window/metric definitions and config differences | tested | `frontend/src/features/compare.tsx`; `backend/app/research_views.py::compare_results`; `tests/integration/test_research_view_http.py`; H4 | Native 2–4 evaluation comparison with incompatible-ranking refusal exists. Portable comparison UI is not generalized. L110 does not explicitly require cross-mode ranking. |
| V33 | L111 | Settings users, secret references, defaults, quotas, permissions, workers, versions, backup state | deferred (management); tested (status subset) | `settings.tsx:35-39,72-75,139-154`; `main.py:282-284`; `models.py:33-45`; `core/config.py:29-34` | Read-only status plus env-managed limits/profiles does not deliver management. No backup receipt surfaced; environment digest is not an inspectable dependency-version list. |
| V34 | L117 | Strategy-specific required fields/lookback, coverage/timezone/prices/actions/shorting/window/budget preflight | tested + deferred | `config.py:104-107,313-355`; `windows.py:184-229,622-630`; `research_services.py:210-285`; H3/H4 | Static OHLCV and task minimum 20/3 sessions checked. No strategy-declared `amount` or custom lookback/shorting requirements. Function supports `required_fields` but production staging never passes a per-strategy declaration. |
| V35 | L121, L123 | Freeze candidate, separate final test, experiment-family exposure across runs/parameters | implemented | `experiment_services.py:157,289,345,632`; `final_evaluation_services.py:320`; `portable_final_services.py:371`; `tests/integration/test_final_evaluations.py`; P5 | Comprehensive implementation exists, including owner-wide cross-family/reimport overlap. Need exact P5. No clean-outside-system holdout claim. |
| V36 | L125 | Training only sees train+warm-up; final separately staged; portable prefix/future checks | tested + implemented | `packages/tradegrad_adapter/windows.py:480-515,610-612`, `final_windows.py`; `workers/research/portable_final_staging.py`; `tests/portable/test_isolation.py:315,407`; H3 + P5 | Native staged-window restriction is not proof against within-window lookahead, correctly labelled. |
| V37 | L129 first clause, L42 | OHLCV plus trades/indicators/linking | tested + deferred | `price-chart.tsx:11-49` only accepts candles/volumes; only call site `data-datasets.tsx:507` | Missing execution markers, indicator overlay and price/result date/listing linkage. This is named functionality, not just styling. |
| V38 | L129 second clause, L43 | Equity/drawdown/monthly return/distribution/training/candidate plots | tested + deferred | Backtest loop only `equity,drawdown` at `backtest-detail.tsx:313`; `research-detail.tsx:718`; `compare.tsx:207` | Training/candidate/equity charts exist. No monthly return heatmap or return-distribution implementation found. |
| V39 | L129 third/fourth clauses | Server filtering/sorting, long-list virtualization, lazy self-hosted Monaco | tested + deferred | `data-table.tsx:21-26`; result-table virtualization; fixed `order_by` in `research_routes.py:118,144,210` and `data_routes.py:61,141,196`; `code-editor.tsx` | Bounded pages/result date-listing filters and virtualization exist. User-selectable server sorting/filtering for the named lists is absent; fixed server ordering is not an interactive sort feature. |
| V40 | L131 | Separate display/raw precision, definitions/fees, windowed downsample, raw download, safe HTML | tested | `research_views.py:60-70,680-725`; `results.py:475-478`; H4 tests/result reports | Existing displayed series satisfy bounded reconstruction. Benchmark absent, tracked separately. Safe script-free generated HTML meets the safety outcome without a report iframe. |
| V41 | L139 | AKShare Shanghai/Shenzhen stock daily raw/qfq/hfq and separate evidence | tested | `providers/akshare.py:63,124`; `marketdata/catalog.py:88`; H2 provider flow/engine bridge | Live Shanghai+Shenzhen acceptance still unverified here. No other-market claim. |
| V42 | L140, L163 | Yahoo daily/Adj Close/dividends/splits; explicit adjust/repair/actions | tested + deferred | `providers/yfinance.py:143-179,192-225`; H2 | Correct split-adjusted labelling, explicit actions and raw SDK retention. `repair=False` is hardcoded and only `provider_native` mode accepted; explicit repair-derived snapshot not delivered. |
| V43 | L141, L143 | OpenBB V5 Nasdaq independent endpoint, unknown prices and complete chain | tested | `providers/openbb.py:96,119,179,239`; `tests/marketdata/test_openbb.py`; H2 | Chain is fixture-backed seed evaluation, not live or corrected short optimization. No unsupported Cboe/TMX enablement. |
| V44 | L145 | Compatible actual OpenBB package versions/build and upgrade contracts | tested | `runtimes/data/requirements.lock`; `script/validate_runtime_split.py`; `docs/M2_OPENBB.md`; H2 | `openbb-build` documented; upgrade live probes remain separate. |
| V45 | L149 | Capability states/scope plus resolve/bars/optional actions/universe | tested + deferred (persisted verification) | `contracts.py:133-159`; provider methods; `catalog.py:13-68`; H2 | Four-state schema enforces live verification scope, but public catalog always documented. Need persisted observed probe receipt to fulfil “last verified” management, not a hardcoded verified flag. |
| V46 | L151 | Full provenance connector/provider/endpoint/params/time/versions | tested | `contracts.py:197-209`; `normalize.py:47-76`; H2 | Present. Terms URL is separate missing clause V101. |
| V47 | L153 | Stable instrument/listing/vendor identity, MIC/type/currency/time/calendar/dates | tested (bounded equity registry) | `models.py:252-307`; `contracts.py:89-123`; `data_services.py:78`; H2 | Explicit provisional symbol+MIC identity does not resolve ticker reuse automatically. Non-equities are rejected instead of misclassified. No need to add tradable derivatives for V1. |
| V48 | L155 | Daily listing/session keys, OHLCV/units/flags, half-open ranges | tested | `contracts.py:175-193,221+`; `normalize.py`; `tests/marketdata/test_core.py`; H2 | Implemented. Minute fields are future extension. |
| V49 | L159, L161, L165 | Explicit adjustment/actions basis; lot-volume conversion; refresh revised history; strict fail closed | tested | `contracts.py:163-172`; AKShare/Yahoo/OpenBB adapters; `tests/marketdata/test_execution_policy.py`; H2 | Unknown OpenBB basis and no strict-action support when evidence absent are permitted by L165. Do not weaken to claim strict support. |
| V50 | L208 benchmark clause, L216 benchmark clause | Configurable pinned benchmark, persisted series and visible basis | deferred | `config.py:311` is `benchmark: None`; `results.py:489-509` always not_configured/false; validator `results.py:1001-1004` rejects benchmark series; `new-experiment.tsx:802` | Unambiguous core result gap. A placeholder object does not implement benchmark support. |
| V51 | L169 | Durable import phases/partial/failure/cancel, block hashes/reuse and all-block publish | tested | `workers/data/executor.py`; `data_services.py:157,340,353,382,402`; `tests/integration/test_data_imports.py`, `tests/system/test_data_worker_recovery.py`; H2 | Implemented. |
| V52 | L171 | Raw/Parquet/manifest/quality/coverage; immutable refreshed versions and pinned runs | tested | `marketdata/storage.py:19-169`; `data-workbench.spec.ts:541-580`; H2 | Raw means uncleaned SDK table, not HTTP bytes, allowed by plan. |
| V53 | L173 | Per-listing completeness; not partial-basket success; classify missing causes | tested (bounded cause evidence) | `marketdata/calendars.py:46`; `normalize.py:115`; `workers/data/executor.py:417-426`; H2 | Holidays/listing bounds/provisional/missing/errors handled. Unknown suspension cause must remain unknown rather than invented. |
| V54 | L175 | No silent fallback/fill; provisional marking/full daily sessions | tested | `normalize.py:189-194`; `marketdata/calendars.py:42`; adapters and partial-import tests; H2 | Implemented. |
| V55 | L181 | Manifest pins data semantics/universe/calendars/mappings/versions/hash; mapped uploads preserve unknowns | tested | `storage.py:135-160`; `normalize.py:222`; upload routes; H2 | Implemented. Additional corporate-action schedule artifacts used by portable have narrower admission. |
| V56 | L185 | Per-upstream bounded budgets/retry; terminal schema/auth errors; secret isolation | tested + implemented | `workers/data/rate_limit.py`; provider call policy/SDK child; `tests/marketdata/test_sdk_paths.py`, `test_execution_policy.py`; PM model transport | Data public endpoints need no account credential. No per-user arbitrary endpoint/secret registration, tracked as management scope, not permission to expose secrets. |
| V57 | L195 | Independent run/attempt/candidate/repair namespaces | tested | `optimizer.py:230-283`; `workers/research/optimization.py:56,346`; `test_optimizer_recovery.py`; H01/H3 | Implemented. |
| V58 | L196 | Explicit CN/US execution config | tested | `config.py:126-206`; evaluator request construction; `test_config_windows.py`, four-task bridge; H3 | Implemented. |
| V59 | L197 | Exit/timeout/cancel/schema truth, warning not failure | tested | runner outcome protocol; `tests/runner/test_runner_contract.py`; `test_security_regressions.py`; H01/H3 | M6 adds regression boundary cases, not a new success definition. |
| V60 | L198 | Only complete compatible checked checkpoints | tested | `checkpoint.py:100,169`; `test_optimizer_recovery.py`; H3 | Implemented. |
| V61 | L199 | Persist recovery state independent of verbose | tested | `checkpoint.py:269,309`; `optimizer.py:199-229`; H3 | Archive/memory/lineage/random/model state persisted. |
| V62 | L200 | Immutable repair versions and failed evidence | tested | `optimizer.py:230-283`; `models.py:424-449`; `research-detail.tsx`; H3/H4 | Implemented. |
| V63 | L201 | Export actual equity/positions/orders/fills/fees | tested | `results.py:333-524`; `test_results.py`; H3 | Aggregate fees available; unavailable component breakdown is not fabricated. |
| V64 | L202 | Bounded parallelism and allowlisted child environment | tested + implemented | runner policy/resource limits; `config.py:237-241`; `docs/M6_FAILURE_MATRIX.md`; H01/H3 and PM | PM process containment needs its specific synthetic gate, no real calls authorized. |
| V65 | L208 excluding benchmark | Immutable complete RunConfig | tested + implemented | `config.py:292-355`; canonical hashes; `test_config_windows.py`; H3; PM model profile hash | Core inputs/windows/costs/objective/optimizer/model/prompt/budgets/env fixed. Strategy parameters embedded in native source rather than declarative contract. |
| V66 | L210, L212 | Market/execution rules, explicit corrected/source-compatible profiles, no paper/broker equivalence | tested | `config.py:126-223,313-355`; `docs/baseline.md`; H3 | Bounded historical native profile, not configurable every order feature. No unjustified paper reproduction claim. |
| V67 | L216 excluding benchmark | Structured result/window/trace/model usage; metric units/definitions/rounding | tested + implemented | `results.py:460-524`; `research_views.py`; `models.py:451-519`; H3/H4 + P5 portable | Model usage sits in durable run/model-call records; evaluator correctly says not applicable. Native observable trace meets L349 allowance. |
| V68 | L218 | Content-addressed cache keys and no uploaded pickle | tested | `tests/baseline/test_cache.py`; `optimizer.py:57-74`; exact snapshot/window/env/strategy hashes; upload schemas; H01/H3 | Implemented. |
| V69 | L222 | Durable model identity, sanitized prompt/response/usage/timing/error/adoption; honest costs | tested + implemented | `model_calls.py:303`; `research_routes.py` model-call reads; `research_views.py::research_usage`; H3 frozen responses, PM operator profiles | No currency price estimate is preferable to fabricated pricing and satisfies conditional requirement. Real provider acceptance remains unverified. |
| V70 | L224 | Iteration/call/token/time/repair budgets, stop_reason/no valid candidate | tested | `config.py:226-241`; `model_calls.py`; `test_optimizer_recovery.py::test_no_usable_strategy_and_budgets`; H3 | Implemented. |
| V71 | L226 | Reuse recorded model replies; uninterrupted/resume candidate equivalence | tested + implemented | `test_optimizer_recovery.py::test_continuous_equals_crash_resume`; `tests/system/test_research_optimization.py`; H3; PM adds unknown-process boundaries | Does not promise deterministic new online output. |
| V72 | L234–245 | Owner/project/provider/instrument/dataset/universe/strategy/experiment/run/attempt/candidate/evaluation/checkpoint/event/artifact/model/outbox entities | tested + implemented + deferred | `models.py` classes at 33,48,72,91,152,188,205,225,252,264,287,309,332,424,451,491,521,551,571 | Most domain entities exist; Project/ProviderConnection entities do not. Owner-private resources satisfy the explicit “owner OR shared” access rule. ProviderConnection management remains required by L103/L111. No need to invent project sharing to satisfy owner access. |
| V73 | L253–263 | Provider/instrument/import/preview/strategy/run/control/events/result/download APIs | tested + implemented | `data_routes.py`, `strategy_routes.py`, `research_routes.py`, `main.py`, final/portable routes; generated OpenAPI | Paths differ from examples but equivalent resources exist. Parameter-only/native contract creation absent (V28); provider connection test absent (V25). |
| V74 | L265 | 202 tasks; structured correlated errors; bounded server lists | tested | `main.py` handlers; `research_routes.py:71-97,100-131`; `data_routes.py`; `backend/tests/test_contracts_storage.py`; H01/H4 | Implemented. User-controlled sort remains separate V39. |
| V75 | L267 | Versioned ordered event envelopes, durable DB authoritative state | tested | `services.py:82`; `models.py:188`; SSE main route; H01/H3/H4 | Implemented. |
| V76 | L273–286 | Recommended modular code organization | implemented | frontend/backend/packages/workers/runtimes/tests/infra tree | Equivalent monorepo responsibilities present. Names are recommendations, not separate product blockers. |
| V77 | L292–296 | Run vs attempt state; checkpoint pause; confirmed cancel; transactional outbox/idempotency | tested + implemented | `services.py:103,238,344,438,459,514`; `research_services.py`; queue/outbox tests; H01/H3, P5 final/portable | Implemented; no exactly-once assertion. |
| V78 | L300–302 | Complete immutable versioned checkpoint state and crash publication reconciliation | tested + implemented | `checkpoint.py:100,169,269,309`; `optimizer.py`; `tests/tradegrad/test_optimizer_recovery.py`; H3 | Orphans are never accepted as completed. Configurable long-term cleanup remains V22. |
| V79 | L306–308 | Heartbeat/lease/generation/sandbox fencing; confirm old stop; whole-tree cancel/recovery | tested + implemented | `services.py:286,316,514`; `workers/research/containment.py`; system recovery; H01/H3, P5 portable, PM model receipts | Must rerun cumulative exact-head gate after overlay; reviewed construction is not kernel execution. |
| V80 | L312, L314 | All resource ceilings; no strategy imports in trusted API/worker; sanitized owned output | tested + implemented | `docs/M6_FAILURE_MATRIX.md` bounded resource table; native/portable runners; H01/H3, P5/PM | Per-artifact byte cap is not total disk quota or representative throughput proof. |
| V81 | L320–322 | Two modes, portable SDK/AKQuant wrapper and independently trusted ledger | tested + implemented | `strategy_sdk/contracts.py`; `runtimes/portable_engine/runtime.py:934`; sibling runtime; `test_portable_research_pipeline.py`; P5 | Correctly separates native untrusted output. Synthetic-only data restriction is hard-coded, not just an unrun smoke: `portable_windows.py:328-340,348+`. |
| V82 | L324 | Portfolio/event templates; LLM changes business rules under fixed infrastructure | implemented + deferred (non-fixture model) | `portable_services.py:114-185,263-271`; `portable_config.py:168-171`; `portable_prompts.py`; source TradeTGD portable pipeline | Both reference templates and actual optimizer machinery exist; only frozen responses admitted. No online portable optimization. This is explicit against broad LLM behavior, not a required paid acceptance call. |
| V83 | L330–333 | PortfolioPolicy, EventPolicy, StateCodec, runtime adapter contracts | implemented | `strategy_sdk/contracts.py:334,443,486,507,511`; `strategy_sdk/state.py`; `portable_engine/runtime.py`; `tests/portable/test_contracts.py`, `test_state.py`; P5 | Existing contract tests; hosted wrapper gate still separate. |
| V84 | L335 | Explicit target basis/omitted positions/timing/rebalance/empty semantics | implemented | `strategy_sdk/contracts.py:443-479`; `portable_engine/runtime.py:401`; portable contract/engine tests | Implemented long-only admitted subset; no empty-dict ambiguity. |
| V85 | L337 | Explicit order fields and capability-gated market/limit subset | implemented | `strategy_sdk/contracts.py:255-283`; portable engine controller; `tests/portable/test_engine.py` | Strict cash market/open-marketable-or-no-touch GTC subset. Advanced order rejection is allowed by the plan, not a V1 gap. |
| V86 | L341 | Manifest timing/lookback/warm-up/calendar and fixed next-open semantics | implemented | `strategy_sdk/contracts.py:56-154`; `portable_config.py`; `portable_engine/runtime.py:49-97,158`; P5 | Present; no silent same-close/next-open conversion. |
| V87 | L343 | Complete multi-security barrier, missing-bar handling, warm-up state only, duplicate handling | implemented | `portable_engine/runtime.py:885`; `tests/portable/test_engine.py::test_daily_barrier_rejects_future_bar_before_prior_session_decision`; isolation prefix tests | Missing input rejects; portable research fixed universe only. Real-data admission limitation remains explicit. |
| V88 | L347 | StrategyManifest code/parameter/data/market/universe/prices/timing/capability/state/env/reference | implemented + deferred (native declaration) | `strategy_sdk/contracts.py:121-154`; `strategy_package.py:619-713`; portable strategy/results | Portable explicit manifest exists; native package records source-embedded parameters and observable capability rather than independently declared preflight requirements. Do not infer native source behavior by static guessing. |
| V89 | L349 | DecisionTrace; native may use observable engine orders/positions/callbacks | tested + implemented | `results.py:494,510-511`; portable result trace; `strategy_package.py:685-686`; H3 native + P5 | Native internal-score absence is explicitly allowed; not a blocker by itself. |
| V90 | L357 | M0 controlled four-task baseline, explicit market, result truth | tested | H01/H3; `tests/baseline`; `docs/baseline.md` | Passed scoped exact heads. |
| V91 | L358 | M1 concurrent ownership/recovery/cancellation gate | tested | H01; `tests/system/test_worker_recovery.py`; isolation tests | Passed scoped exact head; later cumulative regression still needed. |
| V92 | L359, L369, L371 | Three full adapter-to-backtest paths, partial errors/prices/refresh/PIT honesty | tested + implemented (live pending) | `data-workbench.spec.ts:183-440`; engine bridges; H2 | Deterministic source→snapshot→chart→seed is tested. Live mode is not passed. Cold seed execution is not corrected optimization. |
| V93 | L360 | M3 configurable original algorithm/checkpoint/repair/result rebuild | tested | H3; optimizer parity/recovery/result tests | Core implementation passed. Native operator model extension is separate PM. |
| V94 | L361, L393 | M4 no-terminal primary workflow, reload/SSE, persistent plots/diff/access controls | tested + deferred (named missing features) | H4; `configured-research.spec.ts`, `strategy-versions.spec.ts`, result/ownership integration tests | Native primary research browser path exists. Benchmark/management/specified missing charts are not made complete by M4 smoke. |
| V95 | L362 | M5 two portable examples, AKQuant decision comparison, trace/final/export references | implemented | `tests/portable/test_isolation.py:315,407`; `tests/system/test_portable_research_pipeline.py`, `test_portable_final_pipeline.py`; final/package integration and E2E tests; P5 | No explicit “real-provider portable” acceptance in this line. Do not convert this narrow gate into a live-data certification or claim it requires paid calls. |
| V96 | L363 | M6 fault/ownership/resource/restore/upgrade/install acceptance | implemented + deferred | `docs/M6_FAILURE_MATRIX.md`, `M6_VALIDATION.md`; backup test; P6 | Exact-head hosted execution pending in evidence supplied to this audit. Full Compose and operations completeness still code backlog even after P6. |
| V97 | L365 | Short optimization vertical slice, then expand providers/four tasks | tested + deferred (combined evidence) | `configured-research.spec.ts:174-265,678-724`; H3/H4; provider flow `data-workbench.spec.ts:393-439` | One uploaded synthetic-data short optimization exists. No three-provider source→corrected short-optimization→result-chart combined test. This is an integration-evidence gap, not proof APIs cannot support it. |
| V98 | L375–389 | Eight recovery acceptance cases | tested + implemented | Exact per-case map in `docs/M6_FAILURE_MATRIX.md`; H01/H3; P5/P6 additions | Initial native cases passed; full final/portable/current-overlay matrix must pass on accepted head. Do not add local/test totals into an invented unique test count. |
| V99 | L395 performance clause | Metadata/chart/import/evaluation/concurrency on named representative hardware/data; budgets | implemented + deferred (representative acceptance) | `script/benchmark_v1.py`; `script/benchmark_v1_workloads.py`; `docs/M6_PERFORMANCE.md:7-22`; `M6_WORKLOAD_PERFORMANCE.md:20-39` | Prepared five-dimension smoke is genuine but fixed to 76 synthetic rows/one listing; explicitly no representative capacity target. Need agreed workload/hardware and measured budget, not extrapolation from this smoke. |
| V100 | L397, L523 | Frozen fixtures plus small live source probes; upgrade affected contracts/pins without rewriting history | tested + implemented (live pending) | `tests/marketdata/test_live_providers.py`; CI live mode; runtime locks/env/image/data hashes; H2–H4 | Fixtures appropriately primary. Live source gate remains separate. Prior OpenBB history DNS failure recorded; do not label unattempted other probes failed. |
| V101 | L525, L529 | Actual component notices; unresolved TradeGrad public redistribution permission; chart attribution | external-blocked (public distribution); implemented (notices) | `docs/THIRD_PARTY.md`; root/upstream license limitation in `V1_IMPLEMENTATION.md`; `price-chart.tsx:58-69`; FastAPI license | Unresolved upstream license blocks a public distribution claim, not private internal validation. No legal conclusion invented here. |
| V102 | L531 | Separate data rights; manifest terms_url/upstream/acquisition time | tested + deferred | `contracts.py:197-209`; `normalize.py:47-76`; `storage.py:135-160`; no terms_url in these contracts | Provider/upstream and fetched_at present. `source_url` documentation pointer is not a declared terms_url; explicit data-use terms metadata absent. |
| V103 | L533 | Reliable research workbench + immutable inputs/strategies + full results + portable contracts | implemented, not fully accepted | Covered by rows above | No blanket completion until unresolved required feature rows and release gates close. V2 remains outside this audit. |

## Portable scope: exact reading, not speculative expansion

1. L362 explicitly requires portable contracts, two examples, traces, final testing/export, AKQuant decision comparison and complete version references. L397 explicitly favors frozen fixtures. A successful P5 deterministic gate can therefore close that **narrow M5 gate** without proving current public-source connectivity or spending money on models.
2. L320 says portable is a V1 backtesting path, and L324 says the LLM optimizes business rules. Those broader capabilities are currently narrower than a user would infer from “full V1.” `PortableRunConfig` and `portable_services.validate_portable_submission` reject every non-fixture model. `portable_windows.py:328-340` requires `known_none`, `synthetic_by_construction`, `is_synthetic=True`, exact generator `portable.synthetic.daily.v1`; later checks require `portable_fixture` provenance. This rejects even an otherwise genuine, complete raw/no-action upload. It is not merely a missing live smoke or a fail-closed action-window check.
3. The plan does not require *all three providers* to be portable-compatible: L165 explicitly allows unknown/provider-native research and fail-closed strict action handling. Yahoo split-adjusted OHLC and OpenBB unknown bases must not be silently relabelled raw. A bounded real-data portable slice can support one honest raw/evidence-qualified path and keep the other paths rejected.
4. No online call needs to be made by this audit. Implementable portable model admission can be tested against synthetic endpoint responses and fixtures under the already reviewed boundaries, with any real-provider transmission/cost kept separately unauthorized. Whether the full product must expose that admission should be resolved explicitly rather than hidden in a milestone label.

## Prioritized, bounded implementation backlog

These are original-V1 completion slices, not evidence that they are already accepted. All preserve existing immutable artifact versions and remain V1-only. At this checkpoint B1/B8 provider completion, B2 benchmarks, B3 Settings, and B4 full Compose are in isolated local implementation. B5/B6/B7/B9 remain explicit backlog. Every slice still requires review, applicable local checks and hosted integration acceptance; no production deployment, credential changes or paid model calls are included.

### B1. Connect each provider snapshot to corrected short optimization and persistent charts

Plan: L7, L365, L369. Status: integration evidence gap, not demonstrated missing API behavior.

- Extend the three `@provider-flow` paths to use their exact published snapshot in the configured research wizard, not `/runs` cold seed path. Derive/create a matching explicit user-fixed universe, choose original CN cross or US time seed, reserve sufficient warm-up and disjoint train/validation windows, and run one frozen-model iteration.
- Assert nonempty generated candidate/model-call/checkpoint evidence, correct market and immutable input hashes, at least seed plus candidate evaluation, stopped evaluator receipts, result chart rendering and reload from persisted artifacts. Preserve Yahoo/OpenBB semantics warnings. Assert no holdout exposure through optimization.
- First gate uses SDK-schema fixtures and zero paid calls. Then the same bounded case can be opted into public live data only when authorized; failures remain provider-specific failed/unverified evidence, not fallback to fixture.
- Keep the existing original-seed M2 tests as source-compatibility evidence; do not replace or relabel them. Update stale `execution-notices.ts:1-2` text saying warm-up-aware M3 is “not available now”; describe this endpoint as cold-start and link to corrected research.
- Completion evidence: all three same-snapshot flows on real PG/queue/Docker/Chromium, plus an explicit live-result ledger if live mode runs. A small public SDK probe alone does not close the browser pipeline.

### B2. Implement one explicit benchmark contract end-to-end

Plan: L109, L208, L216. Status: required functional omission.

- Introduce a versioned optional benchmark reference with a user-selected immutable same-market/currency dataset/listing and explicit price/total-return basis; keep existing `benchmark=None` historic documents valid and byte-stable.
- Validate coverage/calendar/window, forbid implicit strategy-as-benchmark substitution, freeze normalization/reference start and missing-session policy, and compute an independently described benchmark series and excess-return definitions from that input.
- Persist it in a new compatible ResultBundle schema and package references; extend owner/hash validation, compare-compatibility checks, windowed query/downsampling, report and chart UI. Preserve old “not configured” outputs honestly.
- Test no benchmark, mismatched currency/window, gaps, one documented dividend/split basis, tampering/cross-owner denial, exact result rebuild and browser selection/reload. No live market request or new model call is necessary for implementation tests.
- Completion evidence: wizard-selected explicit benchmark → persisted result → chart/report/compare/export, with versioned definitions and original precision.

### B3. Deliver safe Settings and connection management, not a read-only substitute

Plan: L21, L103, L111, L235. Status: required functional omission.

- Add scoped user management using existing admin/owner authorization (account list/create/deactivate/reset initiation as explicitly designed), protected from ordinary owners. Do not grant self-service persistent external credentials accidentally.
- Manage **references to already provisioned secrets**, not raw credential disclosure. Display admitted provider/model profile metadata and enabled capability boundaries; keep credential values server-only and never echo them. New external credentials/grants are an independent approval boundary.
- Store versioned default research settings and bounded per-user/project quotas; current run configs remain immutable. Reject values above operator hard ceilings. Add audit records and safe idempotent mutations.
- Add an explicit bounded provider connection/probe action with a persisted scoped receipt (endpoint/package/symbol/date/time/outcome), and surface last verified/unknown state without declaring an entire provider globally verified.
- Surface actual dependency versions and last operator backup/restore receipt/status. A receipt may say absent, stale or failed; no invented readiness and no web-accessible backup data.
- Completion evidence: admin/ordinary-user authorization tests, secret-redaction regression, mutation/retry/back/reload browser tests, bounded probe metadata and receipt display. Setting up actual credentials or making paid calls is not a prerequisite for safe fixture tests.

### B4. Supply the promised full single-host Compose install

Plan: L51, L61, L86. Status: required deployment omission.

- Add separately locked API, research coordinator/worker and production frontend images plus data image wiring; Compose includes migrations, API/frontend, database, data dispatcher/worker, research dispatcher/worker/reconciler, and trusted evaluator control.
- Resolve host-daemon bind-path identity explicitly. Candidate containers never receive daemon socket/network/secrets. Only the trusted runner may manage containers; preserve current fail-closed cgroup/seccomp/image-pin checks. Do not “fix” namespace mismatch by broad host mounts or host execution fallback.
- Use durable shared storage paths/volumes, private operator-provided environment references, health dependencies, bounded concurrency and an explicit bootstrap step. No default/static credentials; no automatic account grant or internet exposure.
- Supply production static asset serving instead of `npm run dev`, clean install/start/stop/upgrade instructions, and private cold backup/recovery integration. Keep the existing host development profile as such.
- Completion evidence: fresh disposable installation from documented command sequence, migrations/bootstrap, one full fixture browser research flow, restart persistence, contained cancellation, backup→empty restore→upgrade. Building Dockerfiles alone is not install acceptance.

### B5. Add explicit strategy declarations and parameter versions

Plan: L106, L117, L239, L347. Status: required functional omission for non-reference native strategies.

- Extend version creation with a bounded, versioned declaration of required fields, lookback/warm-up minimum, allowed market/shorting/order capabilities, and optional parameter schema/values. Preserve old seed-source semantics with explicitly derived fixed declarations, not guessed requirements from arbitrary Python.
- Pass required fields/minimum history from the chosen strategy into the existing preflight and evaluator request; reject non-null-field absence as well as missing column names (`amount` currently exists as empty cells in some CSVs). Treat declarations as user claims, not proof of no lookahead/safety.
- Freeze parameter hashes and parent versions, expose them in library/diff/wizard/export, and test an amount-required strategy against OHLCV-only data plus longer-lookback and shorting mismatch cases.
- Portable policy upload/edit can be a separate bounded versioned-contract slice; current reference templates should not be presented as a general portable editor.

### B6. Complete the specifically named analysis/interaction features

Plan: L42–43, L129.

- Join existing persisted fill/order timestamps to bounded listing OHLC data for trade markers and selected-trade navigation; add explicitly declared/retained indicator series rather than recomputing unknown strategy internals.
- Derive monthly return heatmap and return distribution from persisted full-precision equity with versioned timezone/session/return conventions. Window filters must not silently change global metrics; zero/missing equity and partial months need explicit semantics.
- Add server-side sort/filter controls to the promised instrument/candidate/order/fill lists using allowlisted sort keys and bounded pagination. Fixed database order does not count as user sorting.
- Gate with persisted-artifact-only rebuild tests and real browser visible chart/selection/filter/reload tests, not merely ECharts installation.

### B7. Finish the bounded operations baseline and representative measurement

Plan: L92, L94, L395.

- Record/query queue waiting and execution durations, cancel-request-to-confirmed-stop latency, import failure counts/rates and disk usage with declared windows. Label outbox pending separately from actual queued/running jobs.
- Add configurable dry-run-first cleanup of temporary/high-volume logs with immutable/reference checks and explicit retained final/code/checkpoint/data classes. Never auto-delete Run-referenced data. Verify retry/resume and backups remain usable after cleanup.
- Complete the actual M6 restore/upgrade gate. Then agree one finite representative daily workload and named hardware, measure the five required dimensions, and set documented resource/capacity budgets from measured results. The current 76-row single-listing smoke is useful but insufficient for that stated acceptance.

### B8. Close small explicit data-contract omissions

Plan: L163, L531.

- Add a distinct, explicitly requested Yahoo repair-derived import/version with `repair=True`, parent/source references and new provenance/hash; original un-repaired dataset bytes remain unchanged. Keep Yahoo split-adjusted semantics accurate.
- Add immutable provider terms metadata (`terms_url`, upstream and acquisition time) to new dataset manifests/provenance. Do not rewrite already published hashes. An SDK documentation URL is not automatically the governing data-use terms URL.

### B9. Resolve portable production capability boundaries deliberately

Plan: broad L320/L324, with narrow gate qualification L362/L397.

- Minimum honest delivery today: describe portable as synthetic-data, fixture-model research and cite the passed finite decision/wrapper gate only after P5 finishes.
- For broad usable portable V1: add one explicitly evidenced raw-price/no-actions (or implemented-action) ingestion path without the synthetic-generator-only predicate. Provenance must prove action coverage; an empty actions list alone cannot. Continue rejecting Yahoo adjusted/OpenBB unknown histories where unsupported.
- Admit bounded operator model profiles in portable optimization only after carrying the established profile/transport/receipt boundaries into the portable pipeline and synthetic-provider tests. No real call is authorized by this audit.
- Do not add V2 engines, brokers, paper trading, advanced orders or all-provider portable support as collateral work.

### B10. Finish honest release accounting

- Attach final exact-head M5, M6 and operator-model synthetic results. If the source changes afterward, rerun affected/cumulative required gates instead of carrying an old pass forward silently.
- Run bounded live AKShare Shanghai+Shenzhen, Yahoo US, and OpenBB Nasdaq probes/flows only under the separately authorized live-data task. Report exact symbols/date/version/time and failures. Nothing here authorizes paid calls.
- Correct stale top-of-file milestone text (`docs/M3_VALIDATION.md` still opens “no hosted acceptance” but later records H3), global-looking “holdout/portable unavailable” notices in `research_views.py:73-77` and Settings `holdout_execution=False` interpretation. Scope those descriptions to the native optimization endpoint; separate final workflows now exist.
- Do not call reference-only ZIP exports self-contained offline runtimes, or native outputs independently trusted. The plan requires code/data references/parameters/evidence, not bundled licensed raw market data; the current reference-only export is not a gap on that basis.
- Public redistribution remains conditional on upstream licensing. No edit to a README or internal acceptance report resolves that external prerequisite.

## Already covered areas not to reopen without new evidence

The audit found concrete implementation/test coverage for immutable snapshots and original raw SDK retention; no-silent-fallback/price-basis disclosure; manual universe validity and refusal to invent PIT; source-compatible/corrected profiles; real TradeTGD parity; durable candidate/repair lineage; complete checkpoint recovery; no-result/invalid-result failure; authenticated ordered SSE; owner fences; separated final selection/exposure; deterministic owner-scoped package references; and private cold backup/empty-target restore tooling. Their pending exact-head gates must be completed, but they should not be rewritten merely because this audit identifies adjacent missing product features.

No V2 framework, live trading, broker account, all-market/minute data, sophisticated multitenancy, online backup/PITR, or arbitrary advanced order support has been added to the V1 backlog.

## B7 bounded-operations preparation update (2026-10-23)

The operations slice adds owner/admin observations and command-produced historical
backup receipts described in [V1_OPERATIONAL_OBSERVATIONS.md](V1_OPERATIONAL_OBSERVATIONS.md).
V20/V24 measurements and the V33 backup-display portion are now implemented, with
pure checks and prepared exact-stack tests; hosted PostgreSQL/system/Chromium
acceptance is not claimed by this local preparation. Receipt creation/verification
does not prove restoration; stale/version-mismatched observations remain explicit.

V22/B7 cleanup now has an implemented, dry-run-first cold apply command with
cooperative PostgreSQL/root writer exclusion, fresh-plan identity verification,
strict immutable/reference protection and a durable partial-cleanup journal; see
[V1_RETENTION.md](V1_RETENTION.md). This is implementation, not hosted acceptance:
real concurrent-writer/cleanup/restore and full retry/resume-after-cleanup gates
remain required. Existing persistent Run logs remain retained, not relabelled
as disposable high-volume logs. No scope waiver is inferred. The representative workload/hardware/capacity
portion of B7 also remains open; bounded samples and 76-row smoke are not a substitute.

## B9 bounded-admission preparation update (2026-10-23)

The historical synthetic-only/frozen-only description above is superseded for
the bounded implementation in [PORTABLE_OPERATOR_ADMISSION.md](PORTABLE_OPERATOR_ADMISSION.md).
Portable optimization now reuses the default-off owner-scoped operator profile,
real TradeTGD, SDK process, durable budget/usage/replay and confirmed-stop machinery.
Eight synthetic-only hosted SDK/HTTP/PG/Docker cases and the associated browser
selection flow are prepared/collected, not executed locally or accepted as live.

One single-listing raw CSV/Parquet path accepts an explicit immutable user
research assumption, labelled `user_declaration_unverified` / `declared_none`.
It preserves source hashes, exact scope and visible consent, rejects known action
columns, adjusted/unknown semantics and missing/tampered evidence, and never
upgrades ordinary empty action lists or unsupported provider histories. This
does **not** prove real-world action absence, close B9's stronger independently
evidenced action-coverage requirement, or deliver an official three-provider
portable-data path. Actual hosted boundary acceptance remains pending.
