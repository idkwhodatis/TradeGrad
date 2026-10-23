# TradeGrad research workbench · M1 + M2

React + Vite + TypeScript, Tailwind/shadcn components, TanStack Router/Query/Table,
React Hook Form/Zod, and an OpenAPI-generated `@hey-api/openapi-ts` fetch client.
TradingView Lightweight Charts is lazy-loaded for bounded OHLCV preview only.
See `THIRD_PARTY_NOTICES.md` for licenses and attribution.

## Scope and evidence boundaries

M1 remains a real authenticated fixed-seed evaluation slice: login, overview,
strategy catalog, immutable configuration, ordered events, durable attempts,
cancellation, restart-from-scratch resume, metrics and permission-checked artifacts.
Native strategy and engine share a process. Results are research observations,
not an independently trusted ledger. There are no paid models, optimization,
verified holdouts, live trading, account signup or password recovery.

M2 adds:

- Data Sources: scoped provider capabilities and distinct fixture/live transport
  labels; server-side asynchronous security resolution; explicit MIC/listing
  selection and vendor mapping evidence. Calendar-proxy notes are shown beside the
  session calendar, without equating venues. Resolution drafts survive reload.
- Imports: persisted requested/fetching/normalizing/validating/ready states,
  partial/failure/cancellation, per-symbol/date-block coverage and hashes,
  generation-aware retry and completed-checkpoint reuse. Detail fetches at most
  10 blocks per page and labels completion counts as page-scoped.
- Dataset snapshots: explicit provenance, price/split/dividend semantics, quality,
  coverage, immutable parent-version links and full-history refresh into a new ID.
  Single-listing previews request one date window and at most 100 rows per page.
  The optional OHLCV chart uses only that page. Tables preserve original precision.
- CSV/Parquet uploads: explicit date/column mapping, security, MIC, currency,
  calendar, units and price basis; validation errors never become published data.
  Unknown values are preserved, not inferred. Browser uploads are limited to 10 MB.
- Universes: user-fixed, current and historical membership, individual effective
  intervals and known-at timestamps. Current and historical membership require a
  nonblank evidence reference, bounded with its note to a 16 KiB serialized record.
  User evidence cannot create verified PIT.
- Evaluate a chosen immutable snapshot with an unchanged original seed. Dataset
  evidence labels distinguish synthetic demo, provider fixture, live-provider
  snapshot and unverified upload. Native-untrusted results and the launch form
  disclose cold-start windows with no pre-start warm-up. Early history-dependent
  decisions retain original-source behavior; warm-up-aware execution/preflight
  remains an M3 gate, not an implemented M2 feature.

Source capabilities labeled documented or fixture-tested are not live verification.
Synthetic provider fixtures are artificial SDK-schema examples, not captured quotes.
No credentials or fixture-mode switches are accepted from browser payloads.
Original M1 synthetic snapshots remain runnable; they have no normalized M2 preview
or provider-refresh source.

## Development

Use Node >=22.12 (validated on Node 24.19) and npm. Versions and transitive integrities
are fixed in `package-lock.json`. TypeScript 6.0.3 matches openapi-ts 0.99.0.
The scoped TanStack Table v8 API is pinned to 8.21.3. Lightweight Charts is 5.1.0.

```sh
npm ci
npm run dev
```

The dev server binds **127.0.0.1:5173** and proxies `/api` to
`http://127.0.0.1:8000`. Set `API_PROXY_TARGET` when needed. Set
`VITE_DEV_HOST=0.0.0.0` only for an intentionally network-exposed container.
Production serves SPA fallback and API on the same origin. Authentication stays
in headers, not URLs. Sessions and owner-scoped drafts survive tab reload;
expiry clears queries and returns to login. Browser storage is not an XSS boundary.

```sh
# Repository root, SDK-free API environment:
.venv-api/bin/python -m backend.app.export_openapi
# Frontend:
npm run generate-client
npm run format:check
npm run typecheck
npm run build
npm test
npm run test:e2e:list
```

Never hand-edit `src/client/`. Request/response types come from the exported
OpenAPI schema, including UploadMapping. UI display/date helpers are not a second
wire contract. Idempotency keys are owner/operation scoped, survive uncertain
network outcomes, and change with changed inputs. Synchronous guards prevent
multiple DOM submissions before React updates disabled state.

## Native research model profiles

The configured native research wizard now reads owner-scoped operator model
profiles from `/research-capabilities`. An unconfigured deployment remains
fixture-only; even on a configured deployment, the default is the deterministic
synthetic fixture. The UI selects an entire immutable configuration, including
profile ID/hash, provider, model, temperature and per-call output-token limit.
It never accepts a base URL, credential environment name, API key or arbitrary
provider parameters. Changing a profile invalidates preflight and retry identity;
revoked or changed profiles are not silently replaced. Historical fixture
requests retain their original four model fields, preserving existing config
hashes and pending draft/retry bodies. Incompatible or corrupt drafts are not
silently translated into a different pending submission.

Synthetic operator endpoints are labeled separately from non-synthetic profiles
that can make paid calls. The review screen discloses the strategy code and
research context sent to the configured provider, and that token/call/runtime
budgets are not a currency spending cap. Availability is not live-provider
verification. Portable research still admits the fixture only.

An opt-in real-stack browser case checks primary-owner-only profile visibility,
exact preflight payload, reload persistence, switching back to fixture, and a
double-click-protected seed-only submission with the exact synthetic profile:

```sh
E2E_MODEL_PROFILES=1 E2E_MODEL_PROFILE_ID=<primary-owner-only-profile-id> \
  npm run test:e2e -- --grep @operator-model-profile
```

Use the real API, data and research workers, Docker evaluator, and two disposable
accounts described below. The specified operator profile must be synthetic,
enabled for the primary account, and unavailable to the other account. The test
fails closed on a non-synthetic profile. It imports an explicit synthetic dataset
and submits a zero-iteration, zero-model-call run, then verifies persisted profile
identity, no duplicate run, empty model-call history, completed Docker seed
evaluations and reload. This is configured-profile browser/API/queue acceptance,
not SDK/provider-call acceptance; the separate hosted synthetic transport gate
must establish that. Live paid-call acceptance remains a separate authorized
operator gate. Browser test discovery is not execution evidence.

## Real-stack browser acceptance

Playwright uses **real HTTP, PostgreSQL, dispatcher, data worker, research worker
and Docker evaluator**. There are no route mocks, fabricated successful responses,
SQLite substitutes, saved logins or embedded passwords. Before starting these tests,
run migrations, bootstrap disposable accounts and set the server/data workers to
explicit `DATA_PROVIDER_MODE=fixture`. The data spec rejects a mismatched mode instead of
silently making live downloads. Live-provider verification is a separate gate.

Required runtime environment:

- `E2E_EMAIL`, `E2E_PASSWORD`: primary disposable fixture account.
- `E2E_OTHER_EMAIL`, `E2E_OTHER_PASSWORD`: second account for ownership tests.
- `E2E_API_URL`: API origin, default `http://127.0.0.1:8000`.
- `E2E_BASE_URL`: optional already-running frontend origin. Otherwise Playwright
  starts the dev server on loopback, where process/socket permissions allow it.
- `API_PROXY_TARGET`: the frontend's backend origin.
- `E2E_LIVE_PROVIDERS=1`: explicit separate public-provider gate, paired with server
  `DATA_PROVIDER_MODE=live` and `npm run test:e2e -- --grep @provider-flow`. Only the
  three positive provider flows support this mode; deterministic negatives skip.
  This mode requires actual live provenance, a non-synthetic snapshot and no fixture
  banner before the real Docker seed run. Unavailable upstreams fail, never fall
  back to fixtures. A fixture-mode pass is not evidence that this live gate passed.

```sh
.venv-api/bin/python -m backend.app.bootstrap --email "$E2E_EMAIL" --password-env E2E_PASSWORD --with-synthetic-fixtures --with-e2e-fixtures
.venv-api/bin/python -m backend.app.bootstrap --email "$E2E_OTHER_EMAIL" --password-env E2E_OTHER_PASSWORD --with-synthetic-fixtures
# Start the real API, dispatcher, data and research workers via documented infrastructure.
cd frontend
npx playwright install --with-deps chromium
npm run test:e2e
```

The original slow and syntax-error E2E strategy fixtures remain explicitly named
and are never substituted for original seeds. Semantic named locators select the
provider, venue/listing and original seed; fixture row ordering is not a contract.

20 discovered scenarios comprise the 9 M1 cases and 11 M2 cases:

- All three independent provider fixture adapters: browser resolution/import,
  validation/publication, bounded paging/chart, pinned snapshot → actual Docker
  original-seed evaluation. AKShare covers Shanghai and Shenzhen together.
- Partial out-of-window coverage, visible per-block failure and retry proving
  completed block hashes/attempts unchanged, rather than pretending recovery.
- Durable cancellation, refresh preserving the previous version and preview.
- Duplicate/missing upload mapping and successful explicit unknown price semantics.
- Fixed/current/historical universe distinctions and no manual verified-PIT option.
- Cross-user read/write/refresh/listing/universe denial and anonymous upload denial.
- Concurrent same-key import replay, conflict on altered body, real offline failure
  and same-key retry, reload persistence and narrow viewport.

Trace, video, screenshots and HTML reporting are **off**. Only outcome-filtered
JUnit evidence should be published by CI. Never upload raw reports, authenticated
browser artifacts, test environment files, tokens or credential-bearing logs.

## Validation boundary

Typecheck, production build, pure unit tests and Playwright discovery can run
without servers. Discovery is **not a browser acceptance pass**. Chromium,
PostgreSQL, Docker and process-recovery verification run only on the authorized
hosted real-stack gate. A fixture gate never substitutes for a live provider gate.
