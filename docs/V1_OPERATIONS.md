# V1 single-host research workbench operations

## Accepted cumulative fixture evidence, 2026-10-23

The complete fixture gate passed in [run 39942518018](https://github.com/idkwhodatis/TradeGrad/actions/runs/39942518018)
at head `42f6cc25f2e4525dcbaad4e58ee95cf733e286cd`, tree `a99216d3fe10b5c5015627e78cabad1e5de66577`.
See [PC and exact suite counts](V1_IMPLEMENTATION.md#original-v1-cumulative-fixture-evidence-accepted-pc).
This evidence is for that exact source, not live upstreams or production deployment.

The bounded disposable application and independent Compose/restart/cleanup scope
passed, including genuine backup/empty-target restore/upgrade and cold-retention
resume. Retention is dry-run-first bounded explicit apply, not preview-only;
no user-storage cleanup or automatic GC is authorized. Frozen measured engineering
budgets are separate from a future agreed production capacity/SLO profile.
Native same-process evidence and private durable-root assumptions remain explicit.

This is a development/self-hosted research profile, not an internet-facing
production deployment or broker-connected trading service. Follow the exact
stage evidence in [V1_IMPLEMENTATION.md](V1_IMPLEMENTATION.md); a documented
command or collected test is not a passed release gate. Native strategy metrics
are observations from a shared strategy/engine process. Portable execution uses
separate policy/engine containers and a trusted ledger, with only the explicit
cash/daily capability subset described in [M5_VALIDATION.md](M5_VALIDATION.md).
No external-engine certification is claimed.

## Preconditions and trust boundary

- One Linux host with Python 3.12, Node 24, PostgreSQL 18.6 and an already
  authorized local Docker daemon. The coordinator, database role, daemon, image
  builder and durable storage are trusted. Do not expose Docker or database
  management interfaces to research users.
- Docker must provide cgroup-v2 memory/swap/CPU/PID controls and seccomp. Missing
  isolation fails closed. Never grant extra host privileges, enable network in
  evaluator containers, or substitute host execution to make a test pass.
- API, research/data workers, dispatcher/reconciler roles and Docker share the
  same durable storage paths. This profile does not implement a distributed
  object-store worker fleet, high availability, PITR or online backup.
- Provision existing authorized database/account/signing configuration privately.
  There are no default passwords or automatically installed accounts. The API
  reads environment variables, not a sourced shell configuration file. Never
  put credentials in source, command arguments, screenshots or CI artifacts.
- Restrict initial use to loopback. Internet deployment requires separate review
  of TLS, account lifecycle, authentication abuse/rate limits and host operations.
  This work does not perform that deployment or alter security settings.

## Optional full application Compose profile

The existing `infra/compose.yml` remains database-only. For the opt-in complete
application packaging, non-root same-path storage contract, separate role images,
explicit migration/bootstrap and exact-Compose hosted acceptance, use
[V1_COMPOSE.md](V1_COMPOSE.md). Do not combine the two Compose files. The manual
process profile below remains supported; packaging/static checks do not replace
actual Docker, recovery, upgrade or browser release gates.

## Install the separately locked environments

Run from the reviewed checkout. Do not install the original paper's aggregate
requirements into API/research environments.

```sh
python3.12 -m venv .venv-api
.venv-api/bin/python -m pip install --require-hashes -r backend/requirements.lock
python3.12 -m venv .venv-data
.venv-data/bin/python -m pip install --require-hashes -r runtimes/data/requirements.lock
.venv-data/bin/openbb-build
python3.12 -m venv .venv-research
.venv-research/bin/python -m pip install --require-hashes -r runtimes/research/requirements.lock
.venv-api/bin/python -m script.validate_runtime_split --runtime api
.venv-data/bin/python -m script.validate_runtime_split --runtime data
.venv-research/bin/python -m script.validate_runtime_split --runtime research
npm ci --prefix frontend
npm run typecheck --prefix frontend
npm run build --prefix frontend
```

Build only local evaluator images, never publish images containing user data:

```sh
export EVALUATOR_IMAGE=tradegrad-evaluator:v1
export PORTABLE_ENGINE_IMAGE=tradegrad-portable-engine:v1
export PORTABLE_POLICY_IMAGE=tradegrad-portable-policy:v1
native_digest=$(.venv-api/bin/python -m packages.tradegrad_adapter.environment)
portable_digest=$(.venv-research/bin/python -m packages.tradegrad_adapter.portable_environment)
docker build --build-arg "TRADEGRAD_ENVIRONMENT_DIGEST=$native_digest" \
  -t "$EVALUATOR_IMAGE" -f runtimes/evaluator/Dockerfile .
docker build --build-arg "TRADEGRAD_ENVIRONMENT_DIGEST=$portable_digest" \
  -t "$PORTABLE_ENGINE_IMAGE" -f runtimes/portable_engine/Dockerfile .
docker build --build-arg "TRADEGRAD_ENVIRONMENT_DIGEST=$portable_digest" \
  -t "$PORTABLE_POLICY_IMAGE" -f runtimes/portable_policy/Dockerfile .
```

The supervisor resolves image IDs and checks frozen environment identity.
Labels alone do not certify reproducibility or strategy correctness. Preserve
accepted image IDs for checkpoint recovery; rebuilding a tag is not proof that
an old run can resume.

## Provision and start an isolated development instance

Set DATABASE_URL (postgresql+psycopg), SECRET_KEY (random, at least 32 characters)
and, when using the optional development database compose file,
POSTGRES_PASSWORD through your private local environment. Never use a production
database for the following fixture setup. `infra/compose.yml` contains only a
loopback development database, not a complete application deployment.

```sh
# Optional: start the dedicated development database after supplying its config.
docker compose -f infra/compose.yml up -d database
export STORAGE_ROOT="$PWD/var/storage"
export TRADEGRAD_RUNNER_STATE_DIR="$PWD/var/runner"
export DATA_PROVIDER_MODE=fixture
export WORKER_CONCURRENCY=1
export DATA_WORKER_CONCURRENCY=1
# PWD must already be the operator-chosen durable directory on persistent storage.
.venv-api/bin/python -m script.provision_storage --root "$STORAGE_ROOT" --durable-parent "$PWD"
.venv-api/bin/alembic -c backend/alembic.ini upgrade head
# Supply E2E_EMAIL and E2E_PASSWORD privately for this explicit local test account.
.venv-api/bin/python -m backend.app.bootstrap \
  --email "$E2E_EMAIL" --password-env E2E_PASSWORD --with-synthetic-fixtures
```

Run each role as a separately supervised process with the same private
configuration and paths:

```sh
.venv-api/bin/uvicorn backend.app.asgi:app --host 127.0.0.1 --port 8000
.venv-api/bin/python -m workers.research dispatcher
.venv-research/bin/python -m workers.research worker
.venv-api/bin/python -m workers.research reconciler
.venv-data/bin/python -m workers.data dispatcher
.venv-data/bin/python -m workers.data worker
npm run dev --prefix frontend -- --host 127.0.0.1
```

Open the frontend on http://127.0.0.1:5173. It proxies `/api` to the loopback API;
health lives at `/api/v1/health/live` and readiness at `/api/v1/health/ready`.
Readiness does not certify an evaluator image, every upstream provider or a
successful research workflow. Verify the corresponding real-stack gates too.
The browser uses authenticated fetch/SSE, never credentials in event-stream URLs.

Fixture mode is deliberately synthetic. Switching to live data is not part of
this install rehearsal and must retain honest provider-specific coverage,
adjustment and calendar warnings. Configured model admission defaults to
deterministic fixture responses. The separate, disabled-by-default
[operator model profile](V1_OPERATOR_MODELS.md) path supports owner-allowlisted
OpenAI-compatible profiles for native research only, using existing worker-only
secret references. No actual provider credentials, calls or grants are supplied
or verified by this install rehearsal. Portable research remains fixture-only.
Enabling real model transmission or paid calls requires separate operator review;
request/token bounds are not billing ceilings. An unconfirmed model-process stop
blocks recovery even if the worker has died. Follow the receipt and restart
boundary documented there rather than manually releasing an attempt.

## Recovery, shutdown and upgrade

1. Stop accepting new work. Let work finish, or request pause/cancel in the UI.
   Pause completes at a full checkpoint; cancel is not terminal until all owned
   evaluator containers are confirmed stopped. Do not equate a lost HTTP reply,
   worker crash or expired lease with successful cancellation.
2. Keep the independent reconcilers running until old attempts are fenced and
   stopped. An unavailable daemon/storage boundary blocks recovery rather than
   permitting a replacement writer. Never erase runner locks/tombstones while
   any old worker may remain. Diagnose only your recorded process/container IDs.
3. For a cold backup or upgrade, stop API, workers, dispatchers, reconcilers and
   every other writer. Use the [private backup/restore procedure](M6_BACKUP_RESTORE.md).
   It rejects active/queued/leased/unstopped work, requires an explicit quiescence
   acknowledgement, and is not a substitute for actually stopping writers.
4. Preserve the original database, complete immutable storage, private backup,
   exact application reference, dependency locks and accepted runtime images.
   Restore first into a new empty database and new storage root. Never restore
   over the original, delete it, or use a downgrade as an automatic rollback.
5. On the isolated restored copy, run the next migration, verify historical
   config/hash/artifact identities and then run ownership, containment, recovery
   and browser gates. The prepared drill exercises genuine 003_m3 to 004_m5 to 005_settings to 006_provider_connections
   schema upgrade, not `create_all` or a fabricated Alembic stamp.
6. Before restarting, ensure no old deployment can write to the new paths or DB.
   Recheck image IDs and environment compatibility. Historical artifacts remain
   readable, but an incompatible image/checkpoint must refuse resume; do not
   rewrite its frozen config to force acceptance. Only then restart supervised
   roles and admit new work. An unsuccessful restore stays quarantined and is
   retried into another empty target, not silently promoted.

Backups contain private user records, authentication hashes, strategy sources
and possibly licensed market data. Keep them private and under the operator's
existing access/retention controls. Hash verification proves integrity, not
source authenticity. Restoring PostgreSQL archives executes SQL: accept only
trusted operator-controlled archives, never an arbitrary uploaded backup.
No backup, raw logs, browser trace or source data is uploaded by the CI workflow.

## Evidence and capacity

The exact-head workflow uses generated job-local accounts, official PostgreSQL
clients, real PG/Procrastinate, actual evaluator containers and Chromium. The
backup drill owns exactly two randomly named disposable databases. Its CI client
shim verifies the recorded container ID/name/label and streams bounded private
bytes; it never mounts host paths or puts connection secrets in argv.

The [failure matrix](M6_FAILURE_MATRIX.md) names the relevant test layers.
[Read/query measurements](M6_PERFORMANCE.md) and
[fixed fixture workloads](M6_WORKLOAD_PERFORMANCE.md) are reference measurements,
not a production throughput promise. Result reads append exposure records, and
benchmarking them requires explicit acknowledgement. The fixture workload driver
also requires explicit mutation opt-in and actual attempt overlap for its two-run
concurrency observation. Neither invents performance numbers when unavailable.
The frozen daily engineering regression budget is measured separately; any
production capacity/SLO profile still requires agreed workload/hardware and its
own measurements. Shared hosted-runner timings do not certify a user's host.

## Bounded operations evidence

Overview and Settings now expose bounded owner/admin operational observations and
sanitized operator-produced backup-command receipts. See
[V1_OPERATIONAL_OBSERVATIONS.md](V1_OPERATIONAL_OBSERVATIONS.md) for exact windows,
limits, unavailable/stale semantics, private receipt registration and exact PC evidence.
[Bounded retention](V1_RETENTION.md) is dry-run-first explicit apply with cooperative
writer exclusion and accepted disposable cleanup-to-same-checkpoint-resume evidence.
No production/user-storage cleanup or automatic GC is authorized.

Owner-local public adapter preferences and their separate frozen evidence contract are documented in [Provider connections](V1_PROVIDER_CONNECTIONS.md).
