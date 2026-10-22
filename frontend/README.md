# TradeGrad research workbench · M1

React + Vite + TypeScript, Tailwind/shadcn components, TanStack Router/Query,
React Hook Form/Zod, and an OpenAPI-generated `@hey-api/openapi-ts` fetch client.
See `THIRD_PARTY_NOTICES.md` for the official FastAPI template attribution.

## Scope

This is a real authenticated **synthetic fixed-seed evaluation** slice, not a full
V1 product or a mock optimizer. It includes login, overview, immutable dataset and
strategy catalogs, new evaluation, ordered event replay, durable run/attempt status,
cancel, restart-from-scratch resume, numeric metrics and permission-checked downloads.
Native strategy and engine share a process. Results are research observations, not
an independently trusted ledger. No data providers, paid models, account signup,
password recovery, holdout claims, optimization charts, or live execution are shipped.

## Development

Use Node >=22.12 (validated locally on Node 24.19) and npm. Versions and transitive
integrities are fixed in `package-lock.json`. TypeScript is pinned to 6.0.3 because
@hey-api/openapi-ts 0.99.0 requires its compiler API; TypeScript 7's native entry
point is incompatible. `@hookform/resolvers` is pinned to 5.9.1 because 6.0.0's
optional Effect peers do not currently resolve cleanly. Peer checks were not bypassed.

```sh
npm ci
npm run dev
```

The dev server binds **127.0.0.1:5173** and proxies `/api` to
`http://127.0.0.1:8000`. Set `API_PROXY_TARGET` when needed. Use an explicit
`VITE_DEV_HOST=0.0.0.0` only in a container needing network exposure. Production must
serve the SPA fallback and API on the same origin. Tokens are not placed in URLs;
SSE and artifact downloads use Authorization headers. Sessions are tab-scoped,
expire automatically, and survive reload. Browser storage is not an XSS boundary.

```sh
# From repository root after changing backend schemas:
.venv-api/bin/python -m backend.app.export_openapi
# From frontend:
npm run generate-client
npm run typecheck
npm run build
npm test
npm run test:e2e:list
```

Generated files under `src/client/` must not be hand-edited. Request/response types
come from `../packages/contracts/openapi.json`. UI-only validators are not a second
server contract. Idempotency keys survive network failures/reload and are scoped to
an authenticated owner; a changed configuration receives a new key.

## Real-stack browser acceptance

Playwright scenarios use **real HTTP, PostgreSQL, dispatcher, research worker and
Docker evaluator**. There are no route mocks or SQLite substitutes. Start the stack
using the repository's infrastructure instructions, migrate it, and bootstrap two
synthetic-only test accounts. Never run these fixtures on a real-data environment.

Required environment variables:

- `E2E_EMAIL`, `E2E_PASSWORD`: primary synthetic fixture account.
- `E2E_OTHER_EMAIL`, `E2E_OTHER_PASSWORD`: second account for ownership tests.
- `E2E_API_URL`: API origin, default `http://127.0.0.1:8000`.
- `E2E_BASE_URL`: optional already-running frontend origin. If omitted, Playwright
  starts `npm run dev` itself at `http://127.0.0.1:5173`.
- `API_PROXY_TARGET`: dev frontend's backend target, default same as above.

From the repository root (with the API environment configured):

```sh
.venv-api/bin/python -m backend.app.bootstrap --email "$E2E_EMAIL" --password-env E2E_PASSWORD --with-synthetic-fixtures --with-e2e-fixtures
.venv-api/bin/python -m backend.app.bootstrap --email "$E2E_OTHER_EMAIL" --password-env E2E_OTHER_PASSWORD --with-synthetic-fixtures
cd frontend
npx playwright install --with-deps chromium
npm run test:e2e
```

The opt-in E2E fixture seeds are named exactly:
`E2E slow cn_time seed (20s import delay)` and `E2E syntax-error cn_time seed`.
They are actually executed inside the evaluator and never alter original seeds.
They let the browser test running cancellation and failed-run retry without timing
races or fabricated API responses. Do not enable them in ordinary bootstrap.

Scenarios cover login failure/success, repeated UI and concurrent HTTP submissions,
state/events/result/download persistence, running cancellation/resume, failed retry,
session expiry/deep-link return, cross-owner resource denial, direct URL/back/refresh,
small-screen navigation, and actual offline transport failure/recovery. One worker
keeps count assertions isolated; do not run parallel suites against those accounts.

Trace and video are **off** so authenticated request/password bodies are not uploaded.
Failure screenshots, HTML report and JUnit are enabled and must contain only synthetic
test accounts/data. Do not upload test environment files or logs containing secrets.

## Validation boundary

Local typecheck/build and pure behavior tests have been executed. Playwright discovery
validates test loading, **not browser acceptance**. Browser, PostgreSQL and Docker
runtime execution require an authorized runner with supported process/network access.
No browser acceptance pass is claimed until that real-stack gate runs successfully.
