# B7 operational observations and backup-command receipts

This completion slice implements bounded historical observations, not a production
monitoring platform or release-readiness certificate. It does not enable a cleanup
scheduler. Destructive retention remains a separate required completion stage; see
[V1_RETENTION.md](V1_RETENTION.md) for the fail-closed preview and exact writer map.

## Read-only observations

`GET /api/v1/operations?scope=owner&days=7` requires the existing authenticated active
account. Owner filtering applies to every research/import/model query. `scope=deployment`
requires an administrator; a non-admin receives 403, not another owner's data. No user
IDs, worker IDs, raw errors/logs, strategy content, dataset bytes, queue arguments,
credentials, paths or backup inventory are returned. There is no mutation endpoint.

The Overview and Settings reuse the existing Panel/loading/error primitives. They
show a 1/7/31-day window, observation time, 30-second refresh, unavailable values,
partial-sample warnings and a stale banner after 60 seconds. A failed refresh replaces
previous observations with an error/retry state instead of displaying cached success.

Definitions and bounds:

- Current domain states are sampled independently of the history window, up to the
  oldest 10,000 active research rows and 10,000 active import rows. Queued runs and
  requested imports include already-dispatched work; `/status` still separately labels
  pending outbox deliveries. Active research states are preparing/running/finalizing/
  pause_requested/cancel_requested. Active imports are fetching/normalizing/validating.
  Paused and interrupted work is not active. Above the cap, every count is a lower
  bound, not an exact backlog. Queue age is logical submission age, not dispatch age.
- Latest 2,000 attempts **started in the window**: queue wait is start minus the
  matching generation's outbox creation. Completed-attempt runtime is finish minus
  start, not whole logical Run duration. Unfinished, missing and negative values are
  unavailable rather than zero. Mean/max and nearest-rank p95 use valid observations.
- Cancellation latency is cancel-request event to recorded attempt finish, only for
  terminal cancelled attempts with `sandbox_stopped=true`. An unconfirmed stop is
  unavailable. Immediate cancellation of queued/paused work has no separate request
  event and is not invented as a zero-latency observation. This is recorded control
  evidence, not a fresh independent process/container containment check.
- Latest 2,000 terminal import states updated in the window: failed / (ready + failed
  + partial). Partial counts and (failed + partial) fraction are separate. Cancelled
  is counted but excluded from both denominators. Retry history is not reconstructed
  from the mutable current import row, so these are not per-attempt historical rates.
- Latest 2,000 model-call records created in the window include unadopted calls.
  Missing/invalid input/output counts remain unknown; invalid or missing local charged
  counts are unknown too. Charged/reserved tokens are budget accounting, not currency,
  an invoice, or proof of the provider's actual token usage.
- Administrators can see filesystem capacity/free/available counters for the existing
  storage root. The API opens directories without symlink traversal and uses fstatvfs;
  it never inventories or reads files. This is shared filesystem usage, not the size of
  TradeGrad, a user's data, backup storage or a directory tree. Non-admins see restricted;
  absent/unsafe storage returns unavailable without exposing the path.
- Five read queries plus SET LOCAL have a 2-second PostgreSQL statement deadline each.
  Each query is row-capped; a timeout/database error produces a sanitized 503. This is
  a bounded sequential observation, not a repeatable-read whole-deployment snapshot.
  Queries can still reach the deadline on very large histories without new indexes.

These bounded samples do not replace the original plan's representative workload,
named hardware, five-dimension performance measurement, or capacity budget. No such
numbers were invented or inferred from the 76-row synthetic fixture.

## Actual command receipts, not a backup boolean

The existing private operator command accepts optional `--receipt-output /operator/path/new.json`:

```sh
python -m script.backup_v1 create --backup "$PRIVATE_NEW_BACKUP" \
  --storage "$STORAGE_ROOT" --durable-parent "$DURABLE_PARENT" \
  --application-ref "$EXACT_RELEASE_SHA" --acknowledge-quiesced \
  --receipt-output "$NEW_RECEIPT_OUTPUT"
# verify and restore accept the same optional --receipt-output argument.
# Restore still requires a new empty target and --trust-backup-sql.
```

The full existing backup/restore safety contract in [M6_BACKUP_RESTORE.md](M6_BACKUP_RESTORE.md)
continues to apply. No credentials, archives or user data should be supplied in argv,
committed, logged, displayed or uploaded. The receipt is a small sanitized JSON object
written to a new file outside the backup/application storage; it does not include
archive paths, database identities, private inventory, row counts or credential data.
No existing receipt is overwritten. Keep command output private unless intentionally
publishing this sanitized metadata. An incomplete output or failed command is not a
registered receipt.

A receipt is produced only after the requested create/verify/restore command returned
success. It binds the exact manifest-file bytes (not a reconstructed hash), application
SHA, schema revision, backup creation timestamp, observed command timestamp and exact
operation. `create` performs integrity verification internally but remains a `create`
receipt. Neither `create` nor `verify` is ever translated into a `restore` observation.
Receipt output failure is reported separately from a success claim; inspect the private
result rather than assuming rollback or overwriting a completed destination.

To register only that sanitized metadata with an API deployment, the operator sets:

- `OPERATIONS_APPLICATION_REF`: the exact Git SHA of that deployment, not a tag/URL.
- `OPERATIONS_BACKUP_RECEIPTS`: a JSON array containing up to 16 command-produced receipt
  objects. It is parsed as strict typed metadata; the API never follows a path or opens
  an archive. Do not put the private manifest or database configuration in this array.
- `OPERATIONS_RECEIPT_MAX_AGE_SECONDS`: default 86,400; supported range 60–2,592,000.

This metadata is trusted **operator-provided attestation**, not a signature or an
independent proof of source authenticity. The deployment SHA is operator configured;
this slice does not attest a live process's loaded source tree. No API endpoint can
register a receipt, trigger backup/restore, alter credentials or read private paths.

Settings returns receipt metadata to administrators only. Other accounts see
`restricted` and an empty evidence list. No receipt means `not_reported`, not healthy.
Every registered observation is independently classified:

- `observed`: expected deployment SHA and current DB schema match, and both operation
  and backup age are within the threshold. It says nothing about current readiness.
- `stale`: either operation or backup is too old. Re-verifying an old backup does not
  make the source data current.
- `version_mismatch`: application SHA or DB schema differs. A genuine old-schema restore
  followed by upgrade remains historical evidence, not a current-schema restore.
- `identity_unavailable`: deployment application SHA is not configured.
- `invalid_time`: future receipt or backup created after the observation.

Even a recent, matching `restore` observation is only a past successful cold restore.
It never certifies today's data coverage, current recoverability, deployment health,
accepted image identities, hardware capacity, or restart safety.

## Validation status and required hosted gates

Pure arithmetic, ownership-predicate SQL construction, receipt schema/CLI failure and
actual temporary-file safety tests are locally executable. They are not substitutes
for PostgreSQL, pg_dump/pg_restore, real worker execution or Chromium acceptance.

Prepared real tests:

- `tests/integration/test_operations_observations.py`: real PG/API owner isolation,
  global admin scope, generation wait/runtime/cancel timing, import rates, model usage,
  truncation, unconfirmed stops and receipt visibility/staleness/version mismatch.
- `tests/integration/test_backup_restore.py`: real old-schema restore/upgrade and Settings
  cold restore, with exact manifest-bound create/verify/restore receipt assertions.
- `tests/integration/test_retention_preview.py`: real PG reference/active-work gates;
  it does not test deletion because apply is blocked.
- `tests/system/test_operations_pipeline.py`: set
  `TRADEGRAD_REQUIRE_OPERATIONS_INTEGRATION=1` against an already-running disposable
  loopback stack after actual import/research workloads; it requires real observed
  attempt/import evidence and verifies authorization. No local service startup fallback.
- `frontend/tests/e2e/operations.spec.ts`: real login, Overview/Settings/reload/window
  controls, owner isolation and offline failure/recovery. No page.route API mocks.

All real PostgreSQL/system/browser tests were only collected here. Local sockets,
PostgreSQL, Docker, browser and strategy/SDK subprocess execution were not attempted.
No deletion, production backup, credentials, paid model requests, live trades,
workflow edits, commits, publication or deployment were performed.

Combined-tree preparation passed 341 pure Python tests and 425 frontend unit
tests, typecheck and production build. Five operations/retention-preview/system
cases and two cold restore cases collected only; two Chromium flows listed only.
The hosted ordinary application workflow now explicitly runs the operations
observation check after actual fixture workloads. Its HTTP requests also have a
Linux absolute deadline covering trickled headers/body. Metrics are observations,
not a representative deployment capacity claim. Retention mutation is still
unavailable in this slice until the separate writer-exclusion work is validated.
