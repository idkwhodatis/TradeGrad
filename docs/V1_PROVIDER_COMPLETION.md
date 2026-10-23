# V1 provider workflow completion slice

## Accepted cumulative fixture evidence, 2026-10-23

The complete fixture gate passed in [run 39942518018](https://github.com/idkwhodatis/TradeGrad/actions/runs/39942518018)
at head `42f6cc25f2e4525dcbaad4e58ee95cf733e286cd`, tree `a99216d3fe10b5c5015627e78cabad1e5de66577`.
See [PC and exact suite counts](V1_IMPLEMENTATION.md#original-v1-cumulative-fixture-evidence-accepted-pc).
This evidence is for that exact source, not live upstreams or production deployment.

The provider system group passed all nine cases, with the three provider browser
optimization flows included in ordinary Chromium (45 passed, two separate profile
skips). Actual PG/workers/Docker used generated SDK/model responses through the
same immutable snapshots and original TradeTGD. Earlier collected-only reports
below are historical preparation. Separate live SDK/browser evidence is pending;
the live browser selector exercises seed evaluation, not these optimization cases.

Historical local preparation: implemented and offline-validated against base
`a4500bd1408c8d5b5d7e1227fb4a2e93fc371f27`. The new real PostgreSQL,
Procrastinate, Docker and Chromium acceptance gates are prepared and collected,
**not executed in this environment**. This is not a live-provider, paid-model,
production deployment or whole-V1 acceptance claim.

## Same immutable provider snapshot through corrected optimization

The three independent production adapters still run in the data worker. A new
bounded generated-SDK-response interval, `[2023-07-03, 2024-01-02)`, provides
previous session history and two disjoint research windows for:

- AKShare/Eastmoney: `600000` (Shanghai) and `000001` (Shenzhen), original `cn_cross`.
- yfinance/Yahoo: `AAPL`, original `us_time`.
- OpenBB/Nasdaq: `MSFT`, original `us_time`.

These are **generated synthetic SDK-schema rows**, not captures of market data.
The generator is `provider-research.synthetic.v1`, uses pinned exchange calendar
sessions and a fixed absolute session index, and is recorded in the uncleaned SDK
payload. A date block cannot change the generated price for a given session.
Existing 2024 fixture values and counts remain unchanged: 117 mainland sessions
per listing, 124 US sessions per listing, with the original four-session endpoint
samples preserved. New frozen jobs pin `synthetic-daily.v3`; older fixture jobs
fail the existing generation fence instead of mixing checkpoints. Published old
snapshots are not rewritten, and portable fixture admission is unchanged.

The new gates download through the application, keep that exact DatasetVersion
and manifest hash, create an explicit user-fixed universe, select the unchanged
original seed, use corrected configuration with 20 previous available sessions,
then run one actual unchanged TradeTGD iteration with deterministic model replies.
Training is `[2023-09-01, 2023-10-16)`, validation is
`[2023-10-16, 2023-12-01)`, and the reserved holdout is
`[2023-12-01, 2023-12-29)`. No separate market upload is substituted.

Required assertions include seed plus generated candidate, model-call and complete
checkpoint receipts, four real train/validation Docker evaluations, actual staged
warm-up reports, market/profile identity, stopped evaluator receipts, persisted
ResultBundle hashes, nonempty chart data and browser chart reload. Holdout must
have no tracked exposure before any later optional dataset inspection. Native
results remain untrusted research evidence, not an independently trusted ledger.

Yahoo OHLC remains split-adjusted and not dividend-adjusted; OpenBB split and
dividend bases remain unknown. No price semantics are altered to admit portable
execution. Existing cold-start original-seed flows are retained; their notice now
directs users to New Experiment rather than claiming corrected warm-up is absent.

## Explicit Yahoo repair-derived version

`POST /api/v1/datasets/{id}/yahoo-repair` is available only on an original,
unrepaired yfinance provider-native snapshot with known Yahoo semantics and
immutable raw/provenance artifacts. It creates a new durable import/version:

- Parent dataset ID, manifest SHA-256, content hash, raw artifact hash and provenance
  artifact hash are pinned in `RepairSource` (`yahoo_repair_refetch.v1`).
- Submission and worker preparation both validate the parent. The worker also
  reads and verifies its raw/provenance bytes. Listing identity and vendor mappings
  come from the immutable parent, not a new symbol resolution.
- Explicit SDK parameters are `repair=True`, `auto_adjust=False`,
  `back_adjust=False`, `actions=True`. Price bases are not relabelled.
- Every normalized row records `yahoo_repair_requested`, plus `yahoo_repaired`,
  `yahoo_repair_not_flagged`, or `yahoo_repair_status_unknown` according to the SDK
  `Repaired?` value. Raw SDK flags are retained.
- The original remains byte/hash/mtime-stable. Generic imports cannot silently
  select repair. Re-repair uses the original parent explicitly, not an implicit
  refresh of a derived version. Duplicate submissions recover the same durable job.

This is a **new upstream acquisition with repair enabled**, not an offline
transformation of old Yahoo bytes. Upstream historical revisions may contribute
to differences; the application does not falsely attribute every changed value
to the repair algorithm. The deterministic fixture marks its unchanged values as
not repaired; a separate pure adapter test checks a flagged SDK repair row.

## Terms provenance, not legal acceptance

New acquisition provenance has optional `terms_url` pointers from the actual
upstream, distinct from SDK documentation or software licences:

- Eastmoney: https://about.eastmoney.com/home/protocol
- Yahoo: https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html
- Nasdaq: https://www.nasdaq.com/legal

These authoritative public pages were identified through public search on
2026-10-23. A URL is not evidence of permission to redistribute data, a legal
opinion, or an acceptance record. No agreement is accepted, account created or
credential provisioned by this change. Provider/upstream and acquisition time
remain alongside the pointer. Unknown/unmapped sources retain `null` rather than
an invented terms URL. Old manifest/provenance bytes and hashes are left intact;
normal reads neither backfill nor rewrite their optional metadata.

## Bounded source connection testing

Data Sources exposes an explicit `POST /api/v1/data/providers/{provider}/test`
action. It reuses owner-scoped durable imports, quota locks, the outbox, data
queue, provider retry limits and checkpoints, with kind `probe`. There is no
inline SDK/network work in the API and no dataset publication for a probe.

Only one allowlisted public symbol is admitted: AKShare `600000`/`000001`, or
Yahoo/Nasdaq `AAPL`/`MSFT`; the range must be 1–14 completed calendar days. Runtime
mode, paths, raw credentials and endpoints cannot be supplied by the browser.
Probe/repair jobs pin `data-import.v2`, so an older v1 worker rejects them instead
of treating a probe as an ordinary publishing import. Ordinary imports remain v1.

Receipts persist exact symbol/date scope, actual endpoint/package versions,
checked time, outcome and bounded error codes. A complete fixture produces
`fixture_passed` and `documented`, never `verified`. Only complete live response
evidence with installed package versions may produce a scoped `verified` receipt.
Failures remain failed/incomplete/unknown. The global documented capability
catalog is never promoted from a single test. Catalog `latest_test` is the latest
completed **owner's** receipt; import history retains earlier receipts. Repeating
a test creates a new job rather than overwriting an old receipt. Provider
availability can change after any receipt. No live test was run here.

## Historical local validation and hosted execution contract

Offline checks run here:

- Pure fixture/provider/terms/repair/probe/window-staging tests, existing core
  marketdata/data API contracts and selected OpenBB no-subprocess tests.
- Frontend type checking, source/receipt SSR and existing data-behavior unit tests.
- Production frontend build and deterministic OpenAPI/client generation.
- New PostgreSQL/system tests and three Playwright flows collected only.

Results: 131 pure tests passed, four SDK/live/subprocess tests deselected;
45 frontend unit/SSR tests passed; typecheck and production build passed.
The 19 new PostgreSQL/system tests and three Playwright tests collected
successfully but were not executed. Exact patch hashes are in the handoff. Calendar NumPy
unit-deprecation warnings and the existing large Monaco/ECharts chunk warning
are not live acceptance evidence.

The hosted execution contract used the combined source and matching images:

```sh
# Existing PG test environment, dedicated runtimes, migrations and build inputs
# must be supplied by the caller. No credentials are embedded in this document.
python -m pytest tests/integration/test_provider_completion_http.py
TRADEGRAD_REQUIRE_PROVIDER_RESEARCH=1 python -m pytest \
  tests/system/test_provider_research_pipeline.py
# Existing real fixture API + data/research dispatchers/workers + Docker runner:
cd frontend
npm run test:e2e -- provider-research.spec.ts
```

The system gate also requires `TEST_DATABASE_URL`, `API_PYTHON`, `DATA_PYTHON`,
`RESEARCH_PYTHON` and the normal pinned evaluator image environment. Browser
credentials and URLs use the existing E2E harness. No workflow file, deployment,
strategy execution, service, socket, Docker command or browser was run/changed in
that local implementation task. The tests were subsequently integrated and passed
in PC; collection/skips alone never close acceptance. Current live probes remain
a separate opt-in provider-specific evidence ledger, never fallback to fixtures.

Combined completion-tree check: 131 pure tests passed with four SDK/live/process
paths deliberately excluded, and all 421 frontend unit tests passed. Typecheck,
production build and regenerated combined contracts passed. The 19 PG/system
cases and three Chromium scenarios were collected only. The workflow now
contains an explicit three-adapter-to-TradeTGD system step in addition to the
ordinary real browser suite. Its presence alone was not an execution result;
subsequent hosted fixture evidence is PC. Live-source evidence remains pending.
