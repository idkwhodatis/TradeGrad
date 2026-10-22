# M1 research vertical slice

**This is an initial seed-backtest slice, not complete WebUI V1.** It has no
optimizer/model calls, live-data adapters, portable policy engine, trusted ledger,
holdout selection, broker connections or live trading. M2–M6 remain gated work.

Inputs provisioned by the example below are explicitly synthetic. Native AKQuant
strategy and engine share a process. Reported native metrics are not a trusted
ledger and do not prove an adversarial strategy reported its behavior honestly.

## Supported development topology

- Linux, a non-root coordinator user, Python 3.12, Node 24 and PostgreSQL 18.
- Docker daemon on the same host as the coordinator, with an existing authorized
  local Unix-socket connection. The coordinator is privileged with respect to
  Docker; treat it as trusted infrastructure, never expose its socket to users.
- Docker must report working cgroup-v2 memory/swap/PID/CPU controls and seccomp.
  Missing capabilities or an unavailable image/daemon disable execution. Do not
  disable host security or substitute a raw subprocess to make a check pass.
- API, dispatcher, worker and reconciler are independent processes. Their storage
  root and runner-state directory must be the same persistent filesystem on that
  Docker host. A multi-host fleet is not supported by this M1 runner.
- PostgreSQL is mandatory; SQLite is not a fallback. Redis is not required.

The Docker evaluator gets only immutable inputs, not a writable host directory.
Its root filesystem is read-only, network is disabled, capabilities are dropped,
and its two writable tmpfs mounts have byte and inode limits. Only a bounded
metrics wire record and bounded logs return to the coordinator. Image identity is
resolved before launch. Native-process output is schema checked, not trusted.

## Start a local development instance

Do not run these commands against production. Supply your own local configuration
through the environment; never commit it. No example account is installed at API
startup. Use the synthetic fixtures only for development/testing.

```sh
python3.12 -m venv .venv-api
.venv-api/bin/python -m pip install --require-hashes -r backend/requirements.lock
docker build -t tradegrad-evaluator:v1 -f runtimes/evaluator/Dockerfile .

# Set POSTGRES_PASSWORD, DATABASE_URL and SECRET_KEY in your local environment.
# DATABASE_URL uses postgresql+psycopg://... and SECRET_KEY must be >=32 characters.
docker compose -f infra/compose.yml up -d database
export STORAGE_ROOT="$PWD/var/storage"
export TRADEGRAD_RUNNER_STATE_DIR="$PWD/var/runner"
export EVALUATOR_IMAGE=tradegrad-evaluator:v1
.venv-api/bin/alembic -c backend/alembic.ini upgrade head

# Set E2E_EMAIL/E2E_PASSWORD to an explicitly local development account.
.venv-api/bin/python -m backend.app.bootstrap \
  --email "$E2E_EMAIL" --password-env E2E_PASSWORD --with-synthetic-fixtures

# In separate terminals with the same environment:
.venv-api/bin/uvicorn backend.app.asgi:app --host 127.0.0.1 --port 8000
.venv-api/bin/python -m workers.research dispatcher
.venv-api/bin/python -m workers.research worker
.venv-api/bin/python -m workers.research reconciler

cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

The frontend proxies `/api` to the local API. Authentication uses header bearer
tokens. Tokens are never placed in SSE URLs. This local development setup is not
an internet-facing deployment; TLS, account lifecycle/rate limiting and production
operations belong to later hardening. The frontend session is browser-local and
must be cleared on logout or token expiry.

## Execution and recovery

Run submission, the first durable event and the outbox record commit atomically.
An owner-scoped idempotency key is required. Replaying the same key/body returns
the same run; reusing it with a different body conflicts. The dispatcher inserts
the Procrastinate job and acknowledges its outbox record in one database
transaction. Duplicate job delivery cannot claim a second live attempt.

Each attempt has a new UUID, lease token, generation and storage namespace. An
expired lease cannot publish a result or renew itself. Reconciliation fences the
attempt and releases a retry only after the exact old container is removed and
its absence confirmed. Durable per-attempt stop tombstones and an interprocess
lock prevent delayed starts after recovery. Keep these tombstones while any old
worker process might still exist; do not erase the runner-state directory during
active/recovering work.

Cancellation is a request until process containment is confirmed. If Docker or
the fence volume is unavailable, the attempt remains blocked rather than being
misreported as cancelled. An independent supervisor watchdog enforces the
evaluation budget even while database callbacks are blocked.

M1 **resume re-evaluates the seed from the start**, in a new attempt. It is not an
optimizer checkpoint resume. Model budgets, atomic optimizer checkpoints and
mid-run recovery are M3 work.

## Verification

```sh
.venv-api/bin/python -m pytest tests/runner -q
TEST_DATABASE_URL="$DATABASE_URL" .venv-api/bin/python -m pytest tests/integration -q
TRADEGRAD_REQUIRE_DOCKER=1 .venv-api/bin/python -m pytest tests/isolation -q
# Requires a disposable PostgreSQL role with CREATEDB and the local evaluator image.
TRADEGRAD_REQUIRE_PROCESS_RECOVERY=1 TEST_DATABASE_URL="$DATABASE_URL" \
  .venv-api/bin/python -m pytest tests/system -q
.venv-api/bin/python -m backend.app.export_openapi
cd frontend && npm run generate-client && npm run typecheck && npm run build
npm run test:e2e
```

Use a dedicated disposable PostgreSQL database for tests. Database tests create
and drop only their generated schema, never an arbitrary existing schema. The
opt-in process-recovery test creates and drops a uniquely named temporary database
using the existing test role; it does not create roles or alter permissions. It
SIGKILLs only its own worker process groups and removes only its own evaluator
containers. Its API, dispatcher, reconciler, accounts and storage are isolated
from any separately running browser-test stack. Never target a production server.
The browser suite needs the actual HTTP API, queue roles, database and Docker engine.
It does not route-mock API success. Browser traces and videos are disabled so
authentication traffic is not uploaded in test artifacts.

See `M1_VALIDATION.md` for checks actually run versus checks still pending. A
collected/skipped suite is not a passed gate. Do not proceed to M2 until all M1
runtime gates pass. Retain the original database and storage backups before an
upgrade; a production backup/restore rehearsal has not yet been performed.

## Primary references

- [Docker CLI configuration](https://github.com/docker/cli/blob/master/cli/config/config.go)
- [Docker rootless resource-control requirements](https://docs.docker.com/engine/security/rootless/tips/#limiting-resources)
- [Docker tmpfs bounds](https://docs.docker.com/engine/storage/tmpfs/)
- [Docker runtime resource controls](https://docs.docker.com/engine/containers/resource_constraints/)
- [Template provenance](THIRD_PARTY.md)
