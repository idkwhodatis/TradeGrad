# Original V1 completion: operator-managed model profiles

This is a distinct completion slice for the original V1 “choose a model and
budget, run TradeGrad” requirement, not live-provider acceptance or a V2 feature.
It retains unchanged `src.optimizer.TradeTGD` and the durable intent/response,
checkpoint, budget and replay adapter. Native configured research now admits
operator-configured OpenAI-compatible profiles. The B9 extension carries the same
profile/process/receipt boundary into the separate portable protocol; see
[`PORTABLE_OPERATOR_ADMISSION.md`](PORTABLE_OPERATOR_ADMISSION.md). Capability
entries declare `strategy_modes`. Profiles remain disabled by default, and
portable hosted SDK/container/browser acceptance is prepared, not yet executed.

## Default and operator boundary

- `MODEL_PROFILES_ENABLED` defaults to `false`. With no enabled matching profile,
  capabilities and the wizard offer only the deterministic, network-free fixture.
- `MODEL_PROFILES` is a bounded JSON array read from trusted deployment settings.
  Each profile specifies an ID/label, provider label, model, HTTPS endpoint,
  existing secret environment reference, nonempty explicit owner UUID allowlist,
  temperature, output limit, request timeout and request/response byte caps.
- Endpoint and reference configuration is not an API operation. HTTP requests
  may select only an exact public profile configuration/hash from their own
  capabilities. They cannot supply URLs, environment names, keys, arbitrary
  model parameters or someone else's profile. Unknown and foreign profiles have
  the same admission error. Preflight and submission use the same validation.
- API and worker receive matching registry settings, but only the trusted
  research worker receives the already-provisioned selected credential. API
  enumeration does not resolve or test credentials; “configured” is not a
  provider-availability guarantee. These are deployment/process settings, not an
  instant revocation service: workers cache their settings, and changing only
  the API configuration does not stop a running call. Operators must stop/drain
  affected workers through the normal containment protocol and restart them
  with matching registry settings; per-call routing is not silently hot-reloaded.
  Missing worker credentials fail safely as
  `model_credential_unavailable`. This slice creates no keys or persistent grants.
- Production URLs require HTTPS, port 443, a DNS name and `/v1` or `/api/v1`, with
  no userinfo, query or fragment. Redirects, proxies and inherited SDK settings
  are disabled. Azure-specific routes/queries and arbitrary compatible endpoint
  paths are not supported. Operators must trust and restrict endpoint DNS/egress;
  the allowlist is not a defense against a malicious deployment operator.

The registry schema is `OperatorModelProfile` in
`packages/tradegrad_adapter/model_profiles.py`. No production credentials or real
provider configuration are supplied by this change. Operators must separately
approve any actual data transmission/cost before enabling and using a profile.
The UI labels non-synthetic profiles as potentially paid. Token budgets are not
currency ceilings or a guarantee about a provider's billing policy. A compatible
provider might report hidden usage differently or ignore requested limits. The
application records provider-supplied usage when available, reserves conservative
usage otherwise, and stops further work when reported usage exceeds budget.
This supports bounded Chat Completions with `temperature` and `max_tokens`, not
every model, reasoning-token convention or vendor feature.

## Immutable identity and historical compatibility

Runs persist only the public provider/model, profile ID, profile SHA-256,
temperature and output limit, never the endpoint, environment name or secret.
The fingerprint includes routing, secret reference, owner allowlist and all
bounds. Changed, disabled, removed or no-longer-owned profiles are rejected on
submission, resume and worker preparation. Checkpoints bind the exact RunConfig
and existing environment digest; saved responses replay without new HTTP.

Historical fixture model JSON retains exactly its four original fields:
`provider`, `model`, `credential_ref: null`, `temperature`. New default fields
are not inserted into old fixture config bytes, checkpoint inputs, idempotency
bodies, final-selection inputs or package source hashes. Fixture output remains
the old fixed 4096-token bound. Sealed-head native/portable byte/hash fixtures
and nested-final/checkpoint/package-projection tests guard this compatibility.
No historical immutable evidence is rewritten. These are serialization/readability
and completed-call replay compatibility tests, not deployed cross-release resume
acceptance. This slice adds reviewed runtime inputs to `environment_digest`, so an
old run may correctly refuse resume under the new release even though its original
configuration bytes/hash and checkpoint response history remain readable. Existing
environment/image pins still apply; a deployment upgrade is not permission to
resume under a different runtime. Rotating a secret at the same reference or changing a remote model alias
cannot establish deterministic provider/account behavior; those values are not
secret fingerprints.

## Bounded trusted SDK execution

Only a short-lived trusted research subprocess imports the locked OpenAI SDK.
It never imports or executes strategies, policies, evaluators, TradeTGD, or
application configuration. It gets just the selected credential through a
private stdin pipe; no credential is in argv, inherited environment, durable
configuration or evaluator mounts. Its environment is a fixed minimal allowlist.
Raw exceptions and stderr are not exposed.

- SDK retries are zero; redirects and proxy inheritance are off; requests are
  non-streaming chat completions with exact configured model/temperature/limit.
- Actual request body and raw response body are capped before SDK parsing.
  Compressed responses are rejected rather than unboundedly decompressed.
  Response content, choice shape, usage and request-ID sizes are validated.
- Linux is required for this opt-in path. Before network access, the child sets
  parent-death SIGKILL, verifies its parent PID to close the launch race, and
  installs its own wall alarm. The parent also enforces the smaller of remaining
  run budget and the profile's 1–120-second request bound, with up to 5 seconds
  for kill/reap. This is a whole-request bound, not merely an SDK read timeout.
- Local stop does not cancel a provider-side request or prove billing rollback.
  Timeouts, connection loss, child crashes, uncertain launch/persistence and
  absent durable response remain unknown and conservatively reserved. Explicit
  HTTP/schema/body failures are failed and also conservatively reserved. Neither
  is automatically retried, including 429/5xx. Resuming preserves the same logical
  call identities; deliberately submitting a new experiment/config is a separate
  potentially billed action, not a safe retry of the previous call.
- Known credential values, endpoint and reference are redacted from prompts,
  responses, errors and provider request IDs. SSE exposes only bounded summaries
  and local process receipt IDs. Raw provider error bodies/headers are discarded.

## Cancellation and honest recovery blocking

A durable `model_process_admitted` receipt is committed under the active
Run/Attempt lease fence before launching each model subprocess. A matching
`model_process_stopped` receipt is committed only after confirmed local exit.
This is separate from response persistence and provider outcome.

Cancellation/pause prevents admission of another call. An active call waits for
its bound and stop confirmation; a completed response is persisted before a
control stop, preserving replay. Attempt finalization and the stale-worker
reconciler independently check model stop receipts as well as evaluator stops.
The Run lock prevents a stale worker from admitting a new call while recovery
releases the old attempt. A late matching stop receipt may report exit after
lease expiry, but cannot renew the lease or authorize more execution.

If any admission lacks stop proof, recovery remains blocked. In particular, an
active-call worker SIGKILL can leave an unmatched receipt even though Linux kills
its child. Time passing, worker death, a request timeout or a checkpoint is not
substituted for the missing receipt. The UI displays the blocker and resume is
refused. There is intentionally no automatic force-release API or unsafe
PID/name-based kill fallback. Operators must investigate and contain the exact
execution outside this reviewed automatic recovery path; do not delete receipts,
clear `sandbox_stopped`, or retry the run to manufacture proof. Completed-call
crash recovery remains available when its stop receipt was durably published.

## Historical hosted preparation, not executed locally

`tests/system/test_operator_model_pipeline.py` requires both
`GITHUB_ACTIONS=true` and `TRADEGRAD_REQUIRE_MODEL_INTEGRATION=1`. Its only server is
a bounded synthetic HTTP fixture bound to `127.0.0.1` on a job-local port with
job-local generated credentials. The production registry accepts that HTTP form
only when `MODEL_SYNTHETIC_TEST_MODE=true`, `DATA_PROVIDER_MODE=fixture`, and both
hosted gates are active. It is not an insecure production fallback.

The gate uses the production resolver, actual locked SDK, API, PostgreSQL,
Procrastinate, original TradeTGD and Docker evaluator. Coverage includes request
shape/auth, usage, malformed/oversized/error/timeout outcomes, no retry, redaction,
bounded cancellation, completed-response checkpoint replay, active-child parent
SIGKILL, and stale living worker/stop-receipt blocking. It requires separate
`API_PYTHON`, `RESEARCH_PYTHON`, `DATA_PYTHON`, a disposable `TEST_DATABASE_URL`
with CREATEDB and a reviewed prebuilt evaluator image. Run it with the research
interpreter. PostgreSQL owner/admission tests are in
`tests/integration/test_operator_model_profiles.py`. The optional Playwright flow
in `configured-research.spec.ts` checks the real synthetic-profile wizard,
preflight, reload and exact profile persistence through zero-model-call seeded
execution. It is not browser-to-HTTP-model optimization acceptance; the separate
SDK/application gate exercises nonzero original TradeTGD model calls. No
production URL or credential may be configured for that browser gate.

Locally, only pure in-memory HTTP transport, fake-process/database boundary,
configuration/fixture-compatibility, frontend unit/build and collection checks
were permitted. No local sockets, server, PostgreSQL, Docker, model subprocess,
new strategy execution or real provider request was run. Hosted integration and
actual-provider behavior remain unverified until their exact-head gates run.
No paid-provider availability, model quality, returns, paper reproduction,
exactly-once billing or external-provider cancellation is claimed.


### Local evidence and hosted commands

The available local checks passed: 544 API/runner pure tests, 269 adapter pure
tests, and 408 frontend unit tests. The adapter command excludes the four
engine-executing result/window suites, and the runner command excludes the real
research-result protocol suite. Formatting, typecheck, frontend production build,
29-case Playwright discovery and collection of the 27 new hosted cases also
passed. These counts do not include executed hosted or browser acceptance.
Independent read-only review checked the profile/secret/process/recovery boundary,
old fixture serialization and generated-credential diagnostic hygiene.

Use the existing isolated PostgreSQL integration job for the nine owner/admission
cases:

```sh
.venv-data/bin/python -m pytest tests/integration/test_operator_model_profiles.py -q
```

Run the separate 17-case SDK/application gate with the reviewed evaluator image
built from this exact source/environment manifest. `GITHUB_ACTIONS=true` is
provided by hosted Actions; scope the extra opt-in to this command/step only.
The harness generates and supplies its own synthetic registry and credential to
its own API/worker processes, not the surrounding deployment:

```sh
TRADEGRAD_REQUIRE_MODEL_INTEGRATION=1 \
  API_PYTHON="$PWD/.venv-api/bin/python" \
  RESEARCH_PYTHON="$PWD/.venv-research/bin/python" \
  DATA_PYTHON="$PWD/.venv-data/bin/python" \
  .venv-research/bin/python -m pytest tests/system/test_operator_model_pipeline.py -q
```

Both gates need the disposable `TEST_DATABASE_URL` already described above.
Do not export operator-profile flags or test material into later ordinary
fixture-only Playwright/benchmark steps. The optional single Chromium case uses
`E2E_MODEL_PROFILES=1`, `E2E_MODEL_PROFILE_ID`, the normal disposable two-account
E2E settings, and its own API/research process configuration containing only an
owner-allowlisted synthetic profile. See `frontend/README.md` for its command and
zero-model-call acceptance boundary. No production endpoint or credential is an
input to any of these tests.

The same system module also prepares one browser launcher case, enabled only by
the additional step-scoped `TRADEGRAD_REQUIRE_MODEL_BROWSER=1`. With both flags
set it collects 18 cases: the original 17 plus the real Chromium selector flow.
Install the locked frontend dependencies and Chromium first. The launcher reuses
its own `OperatorModelHarness` synthetic server, disposable database, generated
accounts and API/research/data workers. Node receives only `PATH`, `HOME`, `CI`,
the generated E2E account values, local API/proxy origins and fixed profile flags;
never model keys, registry settings or database credentials. No surrounding
fixture browser/benchmark process is reconfigured.

A test-only Linux subreaper gives Playwright a 300-second global timeout, 15 seconds
for ordinary completion, then up to 15 seconds of SIGINT teardown and 10 seconds
of exact-pidfd descendant cleanup. Only ECHILD proves cleanup complete; detached
Chromium/Vite children are adopted and reaped. The parent allows 355 seconds and
a separate bounded cleanup request if that outer limit fails. A custom silent
reporter requires exactly one passed, non-skipped case; raw child output is
discarded. Success also requires zero synthetic HTTP model requests. Pure
launcher tests validate environment shaping, command selection and mocked
containment/deadline transitions, not actual detached-browser containment. That
proof was unexecuted during local preparation; the later actual hosted result
is recorded below.

After integration on top of the M6 preparation and M5 fixture repair, the
available cumulative suites passed 1903 API/runner/portable tests (69 explicit
skips), 1139 research/portable tests (one skip), and 408 frontend tests. These
overlap and must not be summed. The 331 PostgreSQL/system cases and 29 Playwright
cases were collected, not executed locally. All 36 workflow shell blocks parsed;
model integration/browser flags remain scoped to their dedicated hosted step.
The broad legacy baseline lint command still reports three pre-existing findings
in baseline fixture scripts/tests; changed model Python files pass scoped Ruff.

## Verified native admission gate, 2026-10-23 UTC

[Run 39911259617](https://github.com/idkwhodatis/TradeGrad/actions/runs/39911259617)
passed on exact head `4550134750d6369701e996aeeabb9eb2572cacd6`, tree
`13067a0ab784580867ee31a03e3c5959745bd8c7`. The actual hosted synthetic HTTP/SDK
application group and isolated operator-profile Chromium case passed their
required teardown boundary, followed by the full native/portable application,
ordinary browser and reference-workload regressions.

The verified artifact contains 18/18 dedicated SDK/application/profile-browser
cases, 306 PostgreSQL cases plus the separate restore/upgrade drill, and 52/52
paired-container cases. The ordinary browser suite has 28 passed and one
intentionally profile-gated case; that operator case is actually executed in the
dedicated group, not waived. Both performance reports passed their bounded
predicates and all owned cleanup was confirmed. Overlapping suites are not
added into a fabricated unique-case total.

This verifies native operator admission with actual SDK transport to the owned
synthetic server, not a paid/live provider or real user credential. The portable
operator extension belongs to the later cumulative gate and is not inferred from
this native pass. Disabled-by-default settings and all existing boundaries remain.
