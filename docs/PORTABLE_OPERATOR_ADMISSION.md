# B9: bounded portable model and upload admission

This source slice extends portable research without changing upstream `TradeTGD`,
its recorded model calls, the strict strategy/state protocol, the isolated policy
and trusted engine siblings, T+1 execution, or the no-host-fallback boundary.
It does **not** establish live-provider acceptance, independently proven market
action coverage, deployment readiness, or runtime/cross-framework certification.

## Operator model reuse

- The deterministic network-free fixture remains the default. Model profiles are
  still disabled by default; no registry, secret, permission or setting is enabled
  by this change. Existing operator profiles advertise both supported modes.
- Portable configuration uses the existing strict `ModelConfig`. The browser may
  select only an owner-visible exact profile ID/hash/configuration, never an
  endpoint, key, environment name or arbitrary provider parameters. Historical
  fixture documents retain their four-field representation.
- Preflight, submission and resume check current workspace policy and the frozen
  operator profile. Worker startup checks again, and each physical SDK admission
  repeats workspace/profile validation under the existing owner advisory lock.
- The worker calls the existing `create_model` factory. Actual optimization still
  uses upstream `TradeTGD` via `run_optimization`, with its existing intent/response
  journal, budgets, conservative unknown-usage reservations, replay and no-retry
  failures. No alternate or simplified optimizer is added.
- The same durable `model_process_admitted` / `model_process_stopped` receipts
  fence each SDK process. Model cleanup runs even if a sibling fails to stop.
  Unknown close state blocks recovery; the final attempt receipt independently
  verifies all admitted processes. Resume rejects a missing durable stop receipt
  even when the attempt's aggregate flag says stopped.
- Browser reload restores the exact model choice but requires fresh preflight.
  Stale, removed or foreign profile configurations fail closed. The UI warns that
  non-synthetic profiles may bill, tokens are not currency limits, unknown usage
  remains reserved, and stopping local processes does not reverse remote billing.
  Final evaluation remains model-free and retains its separately frozen source.

Existing endpoint restrictions, worker-only credential resolution, process
containment, request limits and deployment-policy caveats are unchanged. See
[`V1_OPERATOR_MODELS.md`](V1_OPERATOR_MODELS.md).

## One explicit, unverified upload route

[`PORTABLE_UPLOAD_ASSUMPTIONS.md`](PORTABLE_UPLOAD_ASSUMPTIONS.md) specifies the
new optional single-listing CSV/Parquet declaration. Consent, exact full-range
scope, original bytes and SHA-256, raw semantics and provenance are immutable.
The label is `user_declaration_unverified` / `declared_none`, never verified
`known_none` or synthetic construction proof. It is exposed in preflight and
research/final result queries and warnings. Known action columns, contradictory
actions, adjusted/unknown semantics, gaps and tampering reject admission.
Ordinary empty action arrays and unsupported provider snapshots remain rejected.

The source can now represent a non-synthetic user-declared research assumption,
but such a declaration does not prove real-world action absence. The stronger B9
independently evidenced action-coverage requirement remains open. This is not an
official three-provider portable-data path or a claim about raw-data correctness.

## Local source/contract evidence

Source was reconciled against sealed base
`4ae644c4eb14822e250d40a7e185d81b3d612dac`, preserving B5 custom source/state
authoring and benchmark bindings. No commits, remote writes or workflow changes
were made by this slice. Rebuild reviewed image digests after integration.

Final isolated-tree gates, 2026-10-23 UTC:

| Gate | Outcome | Boundary |
| --- | --- | --- |
| API `pytest backend/tests -q` | 1028 passed | Control/contract tests, including four owned filesystem-writer exit/retry cases; one Starlette deprecation warning |
| Research `pytest tests/portable --ignore=tests/portable/test_isolation.py --ignore=tests/portable/test_upload_admission.py -k 'not runtime_imports_no_akquant_or_pandas' -q` | 924 passed, 1 skipped, 1 deselected | Existing pure/inert tests, actual TradeTGD with inert providers/runners; calendar package absent in this runtime; import probe explicitly excluded |
| Data `pytest tests/portable/test_upload_admission.py -q` | 62 passed | Fabricated CSV/Parquet and immutable staging; exchange-calendar deprecation warnings |
| Data `pytest tests/portable/test_engine.py::test_offline_calendar_verification_when_data_calendar_is_installed -q` | 1 passed | Separately exercised the calendar check skipped by the research environment; no engine process |
| Frontend `npm test` | 447 passed, 30 files | Unit/render tests, not a browser |
| OpenAPI/client generation, TypeScript, production build | Passed | Build warns about large existing chunks; generated client left verbatim |
| Targeted Ruff/format, Prettier and whitespace checks | Passed | Source-only checks |

Counts describe separate commands, not an invented cumulative acceptance count.
The handoff records exact source hashes and commands. Prior-base results do not
replace this final-tree ledger. Main integration must rerun affected gates after
all other slices and regenerate the shared OpenAPI/client once.

## Prepared hosted gates, not run here

`tests/system/test_portable_operator_pipeline.py` collects nine real-boundary
cases: CN event and US portfolio SDK optimization/pause/replay, malformed HTTP,
server error, timeout uncertainty, cancellation, both zero-budget boundaries,
and the portable profile-selection Chromium case.
The harness reuses the native synthetic HTTP fixture and real SDK process with
actual API/PostgreSQL/queue/workers/TradeTGD and both portable Docker images.
It requires all of `GITHUB_ACTIONS=true`,
`TRADEGRAD_REQUIRE_MODEL_INTEGRATION=1`,
`TRADEGRAD_REQUIRE_PORTABLE_MODEL_INTEGRATION=1`, a `CREATEDB` test database,
three locked interpreter paths and exact rebuilt image identities. It does not
use real endpoints, credentials, paid calls, host candidates or fake fallbacks.

The selected Playwright files collect four cases: the existing portable full
workflow, a new explicitly synthetic operator-profile seed-only selection/reload
case, and two new upload/declaration cases. The operator browser case requires
`E2E_MODEL_PROFILES=1` and a pre-provisioned synthetic `E2E_MODEL_PROFILE_ID`.
It performs zero model calls; the separate system gate exercises real SDK calls.
The upload case uses fabricated observations, not evidence about real securities.

The ninth portable system case additionally requires
`TRADEGRAD_REQUIRE_MODEL_BROWSER=1`. It runs through the existing synthetic
owner/API/server/account fixture and bounded Linux subreaper, not the general
fixture/Compose browser launcher that intentionally disables profiles. The
allowlisted `native_akquant` / `portable` selector fixes the case's file and tag;
unsupported selectors fail before launch. Only generated login material, public
API origins and the validated public selector enter the browser, never model
endpoints, secrets or registry settings. The silent reporter requires exactly
one non-retried successful case from that selected file and tag. The historical
native target remains exactly 18 cases; the cumulative portable target is nine.

Selector follow-up checks: 24 inert Python launcher tests and 15 reporter unit
tests passed; TypeScript passed; collection confirmed native 18 plus portable 9,
and each selected browser command collected exactly one case. These are pure
checks/collection, not an actual browser or hosted-boundary pass.

Only collection was performed. Local sockets, PostgreSQL, Docker, browser and
new candidate/wrapper/SDK subprocess execution were denied or unavailable and
were not retried or bypassed. No live/paid model call, production secret,
deployment, security-setting change, commit, merge or PR comment occurred.

## Main-tree integration checkpoint

Integrated after native result exploration on 2026-10-23 UTC. Available checks
passed: 1,196 backend/operations/Compose-helper tests (including four owned
filesystem-writer exit/retry cases), 924 portable tests with one calendar-package
skip and one explicit import-probe deselection, 62 upload admission tests, and
454 frontend tests. TypeScript, production build, formatting, targeted Ruff and
47 workflow shell-block syntax checks passed. The eight new real-boundary
cases collected successfully; the workflow explicitly opts them in and retains
only sanitized outcome evidence. They have not run locally. The portable profile
browser's dedicated synthetic-launcher selector still needs the separately
reviewed follow-on; ordinary fixture browser mode cannot exercise an enabled
operator profile. No live-model, independent action-data or release claim follows
from these local checks.

The selector follow-on is now integrated. The portable hosted step explicitly
sets its model/browser opt-ins and collects nine cases; the historical native
step retains eighteen. Main integration passed 24 inert launcher tests, 469
frontend tests (including 15 reporter contract cases), TypeScript and Ruff.
All 27 native/portable hosted cases collected only. Neither a skipped profile
case nor zero selected cases can satisfy the dedicated reporter. Actual
synthetic SDK/Chromium results remain pending on the final exact tree.
