# Opt-in full V1 Compose profile

`infra/compose.full.yml` packages the original V1 single-host application. It is
separate from the existing **database-only** `infra/compose.yml`; do not combine
the two files or reuse that development database volume accidentally. This is a
prepared self-hosted/hosted-CI profile, not a deployment performed by this change.
Static tests are not evidence that the images built, Docker isolation worked,
or a real browser completed a workflow. The exact-Compose hosted gate below must
pass at the final source revision before claiming application acceptance.

## Roles and boundaries

- `frontend`: built Vite assets served by a non-root, read-only nginx container;
  the only published endpoint is `127.0.0.1:8080` (port configurable). `/api` is
  same-origin; authenticated SSE is unbuffered, uncached and not access-logged.
- `api`: API lock only, no engine/provider/model SDKs, no Docker client/socket,
  no model credentials, and no candidate execution. Readiness checks the migrated
  database; `/health/live` is distinct from `/health/ready`.
- `data-worker`: separately hash-locked data runtime, including generated OpenBB
  client; no Docker control. `data-dispatcher` uses the lean API runtime.
- `research-worker`: separately hash-locked trusted research coordinator, never
  imports generated code. Its reviewed evaluator supervisor owns Docker control.
  `research-dispatcher` has no Docker access. `evaluator-reconciler` has Docker
  access solely for trusted containment/recovery, in a separate service.
- Native evaluator, portable engine and portable policy images are build-only
  Compose profile entries. The existing supervisor creates actual short-lived
  containers with immutable image IDs, read-only staged inputs, no network,
  no secrets/socket, no added capabilities, resource limits and durable fencing.
  Do not start the `images` profile with `up`; use only its explicit build command.
- `migrate` is a one-shot Alembic `upgrade head` gate. `bootstrap` is explicit,
  profile-gated account provisioning, never an application startup side effect.
  Ordinary startup does not create fixture users or administrators.
  On the Settings-complete release, `BOOTSTRAP_SUPERUSER=1` explicitly requests
  `bootstrap --superuser`: a new account may be privileged, but an existing
  ordinary account is never silently promoted. Default is 0.

This separates dependency environments and physical evaluation containers. It
does not introduce an unauthenticated remote runner API, a second job queue, or
pretend the trusted supervisor is an untrusted sandbox. Socket access is
host-equivalent authority, including when the container is otherwise non-root:
only an operator already authorized for that daemon may use this profile.
Read-only-mounting the socket would not constrain its API and is not claimed as
an isolation boundary. API/data services and all candidate containers never
receive it. The shared PostgreSQL role and durable artifact store remain trusted
single-host application infrastructure, not a separate hostile-tenant boundary.

## Host and storage prerequisites

Use an already-authorized **local Linux Docker daemon**, cgroup v2, effective
memory/swap/CPU/PID controls and seccomp. A current Docker Compose v2 with
`build` Dockerfile-specific ignore support, `bind.create_host_path`,
`service_completed_successfully`, and `up --wait` is required. These are checked
by actual hosted config/build/run acceptance, not presumed from YAML parsing.
The profile rejects root execution, remote daemons, rootless/user-namespace
remapping and mismatched image/environment identity. Docker Desktop, remote
contexts and user-remapped storage need separate review; never enable privileged
mode, weaken runner checks, grant a new socket group, or execute candidates on
the host as a workaround.

The operator first chooses an existing durable directory with durable ancestry,
owned by the same non-root UID/primary GID as the application, mode 0700. Set
`TRADEGRAD_STATE_ROOT` to its canonical absolute path. Do not use symlinks,
parent traversal, a temporary directory, spaces, commas or mount delimiters.
`script.compose_runtime prepare` creates and fsyncs `storage` and `runner`
beneath that explicit boundary, refuses incompatible ownership/permissions,
and never chowns a host tree or contacts Docker.

Both leaves are bind-mounted at **the identical absolute path** inside trusted
services. Thus a supervisor's staged `/chosen/state/storage/...` path means the
same object to the host daemon. There is no guessed volume path or host-path
translation. Compose refuses to auto-create a missing host bind source. The
research worker and reconciler share the durable `runner` directory, preserving
locks, tombstones and empty Docker configuration across restarts. Candidates run
with the supervisor's non-root UID/primary GID, not its supplementary socket
group; staged private-file ownership therefore agrees on both sides.

PostgreSQL uses a project-scoped named volume mounted at `/var/lib/postgresql`,
matching the existing PostgreSQL 18 image layout. Keep the same project name
and original volume for an ordinary restart. `down` does not remove it; **do not
use `down --volumes` on an operator instance**. The explicit disposable CI cleanup
is not an operator lifecycle command. Application state and runner fencing are
not in an ephemeral container layer or `/tmp`.

## Private configuration, build and initial start

Use already-provisioned, authorized private environment settings. No production
credential, default password, new account grant or persistent access is supplied.
Never run shell tracing, print resolved `compose config`, or upload container
inspection/logs, dotenv files, full database archives or state directories.
Compose environment credentials remain inspectable by the trusted daemon owner.
This is not a secret-manager integration or a claim of encrypted process config.

Required settings:

- `COMPOSE_PROJECT_NAME`: one stable operator-selected instance identity.
- `TRADEGRAD_RELEASE`: unique reviewed release label; preserve prior images.
- `TRADEGRAD_STATE_ROOT`, `TRADEGRAD_UID`, `TRADEGRAD_GID`: the provisioned directory
  and its existing non-root numeric owner, matching the launching host identity.
- `TRADEGRAD_DOCKER_SOCKET`: existing authorized local Unix socket path;
  `TRADEGRAD_DOCKER_GID`: its existing numeric group. No permission change is made.
- `POSTGRES_PASSWORD`, `SECRET_KEY`: existing private values; `DATABASE_URL` must
  be the matching `postgresql+psycopg` URI for user/database `tradegrad`, hostname
  `database`, port 5432. The URL must percent-encode reserved credential characters.
  Host-loopback or external databases are intentionally rejected in this profile.
- `DOCKER_CLI_IMAGE`: reviewed official `docker@sha256:...` repository digest,
  obtained from the chosen official CLI image, not an arbitrary Docker executable.
- `EVALUATOR_IMAGE`, `PORTABLE_ENGINE_IMAGE`, `PORTABLE_POLICY_IMAGE`: local image
  references unique to the release; no image publication occurs.
- `NATIVE_ENVIRONMENT_DIGEST`, `PORTABLE_ENVIRONMENT_DIGEST`: computed from the
  final source tree, after all completion patches are integrated.

The separately locked application stages use Python 3.12.14, while the frontend
uses the Node 24 line and nginx stable-alpine by default. Optional
`TRADEGRAD_PYTHON_IMAGE`, `TRADEGRAD_NODE_IMAGE` and `TRADEGRAD_NGINX_IMAGE` select
reviewed app/frontend base digests. The hosted gate resolves those official
bases to immutable repository digests before building. Operator defaults and
the unchanged evaluator Python tag are not OCI-digest-pinned by this change. Preserve
built image IDs/references and review/pin upstream base images under the host's
supply-chain process; environment labels alone are not reproducible-build proof.
The Docker CLI stage is mandatory digest-pinned. Installed Python dependencies
retain their existing hash locks and frontend uses the existing `npm ci` lock.
Dockerfile-specific default-deny contexts include only reviewed source/locks and
synthetic baseline fixtures, not local state, `.env`, `.git` or arbitrary uploads.
The existing root evaluator `.dockerignore` and locks remain unchanged.

Clear inherited `DOCKER_HOST`, `DOCKER_CONTEXT` and Docker TLS overrides; the
preparer rejects them. All following Compose commands select the exact local
socket explicitly rather than trusting a saved CLI context.

From the reviewed checkout, with the settings above already present:

```sh
export NATIVE_ENVIRONMENT_DIGEST=$(python -m packages.tradegrad_adapter.environment)
export PORTABLE_ENVIRONMENT_DIGEST=$(python -m packages.tradegrad_adapter.portable_environment)
python -m script.compose_runtime prepare
dc() { docker --host "unix://$TRADEGRAD_DOCKER_SOCKET" compose -f infra/compose.full.yml "$@"; }
# Quiet validation avoids printing interpolated credentials.
dc config --quiet
dc --profile images build \
  native-evaluator portable-engine portable-policy
dc build api data-worker research-worker frontend
dc up -d --wait database
dc run --rm --no-deps migrate
# Supply BOOTSTRAP_EMAIL and BOOTSTRAP_PASSWORD privately for the intended account.
# BOOTSTRAP_SYNTHETIC=1 and BOOTSTRAP_E2E=1 are deliberate test-only opt-ins.
dc run --rm --no-deps bootstrap
dc up -d --wait --wait-timeout 180
```

No installation command in this document was executed locally by this change.
Open the loopback frontend. Every persistent role has a health check; worker
checks are role-level recent PostgreSQL heartbeats, not per-replica attestations.
The authenticated status endpoint also reports pending outbox work. Startup
validates both native and portable evaluator image labels against the exact
packaged source digest and verifies daemon isolation. A missing/mismatched portable
image blocks this full profile rather than silently replacing it with native
execution. Startup readiness does not prove throughput, provider availability,
model quality or successful strategy evaluation.

## Optional existing model profile passthrough

The default is `MODEL_PROFILES_ENABLED=false`, `MODEL_PROFILES=[]`, fixture-only
research and no model secret. On a release containing operator model support,
API and research worker receive only matching `MODEL_PROFILES_ENABLED` and
`MODEL_PROFILES` registry configuration. The authoritative bounded registry
schema and consent/cost limitations are in
[V1_OPERATOR_MODELS.md](V1_OPERATOR_MODELS.md) and `OperatorModelProfile` in
`packages/tradegrad_adapter/model_profiles.py`.

To enable an already-authorized profile, the operator separately provides a
private Compose override that passes **only its exact existing `secret_env`
variable** to `research-worker`. Do not add a whole shell environment or a broad
shared `env_file`, do not put the value in YAML/source, and do not pass it to API,
data services, reconciler, frontend or evaluator images. The base profile has no
secret passthrough and will safely reject an unavailable credential. This change
does not invent a credential, enable a provider, authorize spending/transmission,
or provision persistent access. Never set `MODEL_SYNTHETIC_TEST_MODE` in an
operator Compose instance; its loopback synthetic test harness has a separate
hosted gate. Changes require normal worker drain/containment and consistent
restart; editing API configuration alone is not instant revocation.

## Recovery and upgrade

Follow [V1_OPERATIONS.md](V1_OPERATIONS.md) and the private cold
[M6_BACKUP_RESTORE.md](M6_BACKUP_RESTORE.md) procedure. Stop new admission, let
work finish or request pause/cancel, and leave the independent reconciler alive
until owned sandboxes are confirmed stopped. A worker's exit or stale heartbeat
is not containment proof. Model stop-receipt blockers remain blockers. Never
remove runner state or reset leases to make a deployment green.

Before backup/upgrade stop every writer, preserve the original DB volume,
complete storage and runner state, exact source/locks and accepted evaluator
image IDs. Take a private cold backup with the existing quiescence guards.
Restore to an isolated new empty database and storage target under a different
Compose project, never over the original. Run that release's `migrate` service
(`upgrade head`, including subsequent Settings revisions), rebuild exact
image/environment identities, and run the real isolation/owner/recovery/browser
gates. Only then promote deliberately. Ordinary restart and idempotent migration
are not a historical-schema upgrade test; retain the real M3-to-M5 backup/upgrade
gate. Incompatible historical checkpoints must refuse resume, not have their
frozen hashes rewritten. No automatic downgrade, destructive rollback, public
exposure, live broker execution or deployment is included.

## Exact-Compose hosted acceptance handoff

Add a **separate disposable GitHub-hosted Linux job** at the final source SHA,
not a replacement for the existing process-recovery, actual isolation, historical
backup/upgrade and operator-model gates. The job must have existing non-root
local Docker access, cgroup v2/seccomp and Compose v2. Do not run these commands
in a restricted local executor or against an operator database/daemon.

Required sequence (the parent-owned workflow supplies pinned checkout/setup
Actions, Python 3.12 and Node 24):

1. Install locked frontend dependencies and matched Chromium:
   `npm ci --prefix frontend`, then, from `frontend`,
   `npx playwright install --with-deps chromium`.
2. Run `python -m script.ci_compose_environment` in its own step. It creates only
   job-local random disposable ordinary/admin fixture credentials, masks them before publishing
   to `GITHUB_ENV`, chooses a unique project/state/release identity, and disables
   operator/live-provider fixtures. Never provide account/production secrets.
3. In a later step resolve the chosen official Docker CLI image's repository
   digest using the explicitly selected local socket. In that same step run
   `python -m script.compose_image_evidence --resolve-bases` to resolve the known
   official app/frontend base tags into repository digests for the next step. For example,
   `docker --host "unix://$TRADEGRAD_DOCKER_SOCKET" pull docker:cli`, then use
   `docker --host "unix://$TRADEGRAD_DOCKER_SOCKET" image inspect docker:cli --format '{{index .RepoDigests 0}}'` as
   `DOCKER_CLI_IMAGE`. Export only this public digest to `GITHUB_ENV`. Review the
   upstream choice and preserve the resolved digest with build evidence.
4. Run `bash script/ci_compose_smoke.sh`. It performs quiet real Compose config
   validation, actual evaluator/application image builds, DB health, migration,
   explicit two-account plus separate new-admin bootstrap, all service health checks, authenticated worker
   readiness, the complete existing real Chromium suite through the nginx origin,
   including the opt-in Settings management suite (`TRADEGRAD_REQUIRE_SETTINGS_E2E=1`
   with generated/masked `E2E_ADMIN_EMAIL`/`E2E_ADMIN_PASSWORD`, no admin data fixtures),
   then cold restart and exact artifact metadata **and downloaded byte-hash**
   comparison. Browser processes receive only the necessary fixture login and
   loopback endpoint environment, not database/signing/model credentials.
5. Its trap stops project writers and removes only generated evaluator containers
   whose reviewed labels **and bind paths** match this unique job's storage,
   then deletes only that disposable project's volumes. Failure/uncertain
   containment fails the job. Use an `if: always()` invocation of
   `python -m script.ci_compose_cleanup` as workflow fallback when environment
   generation succeeded; interrupted jobs may need the runner's own teardown.
6. Export only outcome-sanitized JUnit using the existing
   `script/summarize_ci_junit.py` plus the allowlisted public
   `var/ci-evidence/compose-images.json` written by the gate. This contains exact
   built image IDs, environment labels, source SHA, CLI digest, declared base tags
   and selected app/frontend base digests, not container configuration. It labels
   the unchanged evaluator Python base honestly as not OCI-digest-pinned.
   Do not upload Compose config, raw inspect output,
   raw logs, traces, screenshots, profile registry, state or downloaded data.

This gate has no real upstream/model calls and cannot certify paid-provider
availability. Its synthetic workflows do exercise real HTTP, PostgreSQL,
Procrastinate, durable mounts, actual Docker evaluation and the built frontend.
All image/runner failures must be fixed or reported, never skipped or relabeled
as configuration successes. A known failing paired-container gate remains a
release blocker regardless of static Compose results.

### Shared bounded browser launcher and cleanup limits

The real fixture browser gate is reusable by the ordinary shell-started stack:

```sh
# These origins must point to already-running, explicitly owned local services.
# Supply the generated E2E and optional Settings admin environment privately.
TRADEGRAD_REQUIRE_BROWSER=1 python -m script.browser_acceptance
```

Both `E2E_BASE_URL` and `E2E_API_URL` must be explicit `http://127.0.0.1:PORT`
origins. For the ordinary process stack, start/own Vite before this command;
setting `E2E_BASE_URL` intentionally disables Playwright's automatic web server.
Compose already supplies the built nginx frontend. This is a fixture full-suite
gate, not the separately authorized live-provider or operator-model harness.
`TRADEGRAD_REQUIRE_SETTINGS_E2E` and generated admin login variables are on the
allowlist; database/signing/model credentials, proxy variables, Node hooks and
unrelated settings are not. No credentials appear in child command arguments.

The standalone Linux child adapts the reviewed operator-browser launcher. Its
subreaper, parent-death/race checks, exact pidfd signalling and ECHILD stop-proof
functions are unchanged. The complete suite has a 2400-second Playwright global
timeout, 15 seconds for normal teardown, 15 seconds for graceful interruption,
and 10 seconds for repeated owned-descendant kill/reap. The outer owner allows
2455 seconds, then requests cleanup and waits at most 30 more seconds. Exceeding
an outer bound or failing to establish descendant absence fails acceptance,
even if Node exited successfully. Child stdout/stderr are discarded; only the
existing private JUnit file is later reduced to outcome-only evidence.

HTTP acceptance rejects redirects and inherited proxies. JSON is capped at
2 MiB with a signal-backed 10-second absolute request deadline, including
trickled headers/body; artifact download has a 30-second absolute deadline,
50 MB per-artifact cap and 64 MiB aggregate cap. Every request shares the same
180-second operation deadline. Stored restart-proof JSON is bounded too.
Cleanup shares one 180-second work deadline across all Docker queries/actions,
with at most 5 seconds for owned CLI group kill/reap after failure. Listings
are capped at 64 KiB and 256 total discovered containers; inspections at 256 KiB
and 16 mounts. It verifies owned evaluator absence after removal and again after
Compose teardown, plus absence of project-labelled containers. Oversized data,
changed identity, daemon errors, timeout or remaining containers never count as
successful cleanup. These bounds do not promise that a remote daemon operation
was cancelled when its local CLI timed out; that outcome remains unconfirmed.

Pure local preparation checks require pytest and PyYAML as test tooling (not new
application runtime dependencies):

```sh
python -m pip install --require-hashes -r tests/compose/requirements.lock
python -m pytest tests/compose -q
bash -n script/ci_compose_smoke.sh
```

These parse/check YAML and Dockerfiles and exercise non-network configuration,
path/identity rejection and privacy contracts with in-memory test doubles. They
do not invoke Docker, open a socket, start a server or execute strategy code.

The dedicated `compose` workflow job and ordinary application job run independently
after the exact-head four-task baseline passes, in fixture mode. Each owns a
separate hosted runner, database, storage, credentials and image builds; neither
consumes the other's artifacts. A failure in one does not suppress the other's
diagnostic evidence, and both must pass for the full workflow to succeed. There
is no `continue-on-error` or waived acceptance group. Compose generates temporary runner-only credentials,
resolves the official Docker CLI and application base repository digests, and
records built image IDs. It never uploads resolved configuration, private raw
logs, database archives or restart-proof records. Local source checks passed;
real build/start/restart/Chromium acceptance remains unverified until this job
has actually passed at the published revision.

## Disposable operational logs versus immutable Run evidence

Every running full-Compose service explicitly uses Docker's per-container
`json-file` driver with `TRADEGRAD_SERVICE_LOG_MAX_SIZE` (default `10m`) and
`TRADEGRAD_SERVICE_LOG_MAX_FILES` (default `3`). The preparation command rejects
unlimited or malformed settings and bounds them to 64 KiB–100 MiB per file and
1–20 files. These are maximum storage/rotation settings, not a promise of a
minimum retention age. See the [official logging-driver options](https://docs.docker.com/engine/logging/drivers/json-file/).

This only manages discardable service stdout/stderr. It does not delete a
published Run log, result, source, model replay receipt, checkpoint, snapshot or
backup. The separate cold-retention command only removes verified unreferenced
atomic temporaries. Do not directly modify Docker's daemon-managed log files.
No daemon configuration or existing host/container settings are changed by these
source changes; new reviewed container creation is required for changed options.

The hosted Compose gate reads back only each of the eight running services'
logging configuration and checks the exact bounded policy. Its allowlisted JSON
report contains no log contents or environment. Local preparation passed 67
Compose/helper tests, Ruff, shell syntax and whitespace checks. Actual Compose
read-back and rollover behavior have not run locally; the report explicitly
states that it is configuration evidence, not a rollover stress test.
