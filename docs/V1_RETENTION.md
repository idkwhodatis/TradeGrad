# V1 cold retention preview and remaining apply gate

## Status: preview shipped, deletion deliberately not implemented

`python -m script.retention_v1` is a bounded, private operator inspection, not a
GC service. It never writes, renames, chmods or deletes storage entries or DB rows.
`--dry-run` is the default. `--apply` returns exit code 2 with an explicit blocker
before constructing a database connection or opening the storage tree. V22/B7
cleanup acceptance therefore remains **deferred**, not silently accepted by a
successful preview. This is an interim stage, not a permanent waiver.

The concrete missing invariant is shared writer exclusion. Current publishers
hold a filesystem descriptor, create a temporary, fsync, link/rename the final
name, and unlink the temporary. They do not acquire a common lock with cleanup.
A paused/slow writer can own an old temporary; a terminal DB row or expired lease
does not prove that writer stopped. An operator acknowledgement and two equal
snapshots are useful inspection checks, not a proof permitting deletion.

Persistent `logs.txt`, `engine.log`, and `policy.log` under Run artifacts are
immutable Run evidence, not a disposable high-volume log namespace. They remain
retained even when older than the configured log age or lacking a DB reference.
Docker's bounded runtime log rotation is independent and is not this retention
feature. No log namespace or database event/journal retention is invented here.

## Operator procedure

1. Stop admission/API, bootstrap, both dispatchers/reconcilers and all data and
   research workers; confirm their evaluators/containers/processes have stopped.
   Resolve queued, leased, active and unconfirmed-stop records using the existing
   lifecycle recovery procedure. Never falsify DB states to pass inspection.
2. Use a trusted local POSIX storage root and an explicit PostgreSQL TCP URL in a
   private environment variable. The cold-backup connection/schema restrictions
   also apply. Do not pass credentials on the command line or enable SQL logging.
3. Run, for example:

   ```sh
   python -m script.retention_v1 \
     --storage /srv/tradegrad/storage \
     --database-url-env RETENTION_DATABASE_URL \
     --acknowledge-quiesced \
     --temporary-days 7 --log-days 30 \
     --max-files 100000 --max-total-bytes 20000000000 \
     --timeout-seconds 600
   ```

4. Keep the JSON report private: it contains relative storage identifiers, hashes
   and byte counts, but never reads out source, logs, tokens or DB URLs. A report
   is advisory only, not a persisted deletion authorization. Do not feed its paths
   to `rm`, shell globs or another sweeper. A fresh successful backup and verified
   restore drill are still required before any future enabled apply workflow.
5. Exit 0 means a complete preview, **not reclaimed space**. Exit 1 means failed
   inspection; no partial report is returned. Exit 2 means unsupported apply,
   missing acknowledgement or missing connection configuration. Interrupted
   inspection returns 130. CLI exceptions are sanitized.

`temporary_days` and `log_days` are integers from 1 through 36500. Age uses the
newer of `mtime` and `ctime`, so copying, changing permissions, publishing or
backdating mtime cannot make a newly changed file look old. The boundary includes
exactly the configured age. `log_days` controls only an aged-but-retained count.
Future timestamps are retained. File/directory counts, per-file bytes, total
bytes, combined reference-manifest bytes, scan depth (32), and elapsed scan work
are bounded. Blocking filesystem syscalls are not preempted; PostgreSQL inspection
has its own connection/statement timeouts. This is not a hard wall-time watchdog.

## Conservative storage contract

- Reuse `PostgresDatabase.source_snapshot` from cold backup: known schema versions,
  no active/queued/leased imports or runs, stopped evaluators/attempts, and no
  runnable Procrastinate jobs. Read the snapshot again and require equality.
- Permit only the existing `datasets`, `runs`, `data-checkpoints`, `uploads`, and
  `strategy-packages` top-level namespaces. Unknown namespace or schema means
  failure, not a fallback cleanup rule.
- Read through pinned directory descriptors and `O_NOFOLLOW` at every component.
  Reject symlinks, hard links, devices, FIFOs and other special files. Compare
  file/directory inode identity, mode, link count, size, mtime and ctime while
  reading and between two complete hash inventories. Detectable changes fail
  closed; this still does not certify exclusion of every possible writer race.
- Verify all backup snapshot reference classes: artifact/upload/data-block/package
  hashes and sizes, dataset manifests plus declared source/artifact/preview bytes,
  and checkpoint manifests plus every declared checkpoint member. Unknown shapes,
  duplicate manifest fields, missing bytes and wrong hashes/sizes fail closed.
- Preserve entire referenced dataset/checkpoint subtrees, all source/final/result
  files, code, packages, replay journals, staging directories, unknown names and
  orphan immutable bytes. An absent reference is not permission to delete.
- Only exact existing atomic-publisher temporary layouts can be listed as
  `temporary_review_candidates`: hashed uploads (`csv`/`parquet`), hashed data
  checkpoints, hashed package archives, Run `execution-environment.json`, and
  evaluation `engine.log`/`policy.log` temporary names. Owner/Run/attempt/evaluation
  components must have UUID layouts; atomic suffixes must be 32 hex characters.
  Generic `*.tmp`, arbitrary `.staging`, dataset staging and candidate-code
  temporaries are not candidates. Any direct DB reference or protected subtree
  overrides the temporary classification.
- Every listed candidate is still retained. `deleted_files` is always zero and
  `retained_files` always equals the complete inventory size. No API endpoint,
  production scheduler, migration or application configuration change is included.

## Finite next stage: cooperative writer barrier and enabled apply

The next stage must implement and validate the following, then replace the apply
blocker. A PostgreSQL advisory lock alone does not control independent filesystem
writers or guarantee a container stopped after its DB connection disappeared.

### Lock ordering and lifetime

Use one documented, collision-isolated maintenance key for the same database and
storage installation. Admissions/publications acquire its **shared** lock before
any owner advisory lock, package lock, Run/import row lock or queue-row lock.
Cleanup acquires the matching **exclusive** lock before the cold snapshot,
retains it through reference/inode revalidation and unlink/fsync, and releases it
only after the receipt is durable. Use bounded acquisition with a safe refusal,
not an unbounded wait or lock stealing. All paths must use the same ordering.

Transaction-scoped shared locks suit API admission/publication units. Worker
filesystem writes span many transactions and also happen between transactions:
a lock around `claim_attempt` or the final pointer commit is insufficient. A
session/lifecycle guard must span every write and cleanup while leaving heartbeat
transactions workable; a separate durable ownership/stop fence must survive DB
connection loss until all child output writers are confirmed stopped. No process
termination is authorized by this preview or its tests.

Before deletion under the exclusive lock, require no active, queued, leased or
unstopped business work, no runnable queue jobs or pending dispatch which could
restart it, stopped external writers, and a fresh verified reference inventory.
Do not rely only on the current backup query's state checks: explicitly audit
outbox/admission and worker failure windows during the barrier stage.

### Exact writer/admission map to cover

- `backend/app/services.py`: `submit_run`, `resume_run`, `claim_attempt`, lifecycle
  mutators/`reconcile_attempt`; acquire before `advisory_lock` or row locks.
- `backend/app/research_services.py`: `submit_research_run`, `resume_research_run`,
  `begin_evaluation`, `publish_checkpoint` and completion/pointer transactions.
- `backend/app/portable_services.py`: `submit_portable_run`, `resume_portable_run`,
  evaluation admission/completion and image-pinning transactions.
- `backend/app/final_evaluation_services.py` and `portable_final_services.py`:
  `submit_final_evaluation`, `retry_final_evaluation`, evaluation admission and
  final completion. Preserve the shared-before-owner lock order.
- `backend/app/data_services.py`: `submit_import`, `retry_import`, `claim_import`,
  `fence_import`, `reconcile_imports`. `backend/app/data_routes.py:upload` publishes
  the uploaded bytes **after** `submit_import` and **before** `session.commit`;
  the admission lock must cover that entire unit, not return early.
- `backend/app/strategy_package_services.py:create_strategy_package`: lock before
  package/submit locks and keep it through archive publication and DB commit.
- `backend/app/bootstrap.py:main` and `seed_fixtures`: lock before `bootstrap:*`
  and the initial storage provisioning/publication. Direct helper callers must
  not bypass it.
- `workers/research/dispatcher.py:dispatch_outbox/recover_stalled_jobs` and
  `workers/data/dispatcher.py:dispatch_data_outbox/recover_stalled_data_jobs`:
  cover dispatch/redelivery before queue-row locks. Include data/research main
  reconciliation loops, not only API admissions.
- `workers/research/executor.py:execute`, `optimization.py:execute_configured`,
  `portable_optimization.py:execute_portable_configured`,
  `final_evaluation.py:execute_final`, and
  `portable_final_evaluation.py:execute_portable_final`: guard the full attempt
  lifecycle, including failure/finally paths and confirmed external writer stop.
- `workers/data/executor.py:execute`: guard `_save_checkpoint`, `_fetch_blocks`,
  `_write_universe`, `_publish` and staging cleanup. `_publish` writes most bytes
  outside its final DB transaction, then renames the dataset before commit.

### Non-PG publication and external writer boundaries

These helpers do not acquire database locks and cannot safely authorize their
own cleanup. Their callers must prove the lifecycle guard or a compatible
filesystem exclusion protocol; standalone/offline use cannot silently bypass it:

- `backend/app/core/atomic_storage.py:publish_bytes`, `provision_directory`,
  `sync_tree`.
- `packages/tradegrad_adapter/checkpoint.py:atomic_json/CheckpointStore.commit`,
  `optimizer.py:_publish_code`, `model_calls.py:RecordedModel._save`. Preserve
  candidate source, checkpoints and model replay journals, including interrupted
  publication; they are not high-volume disposable logs.
- `workers/research/executor.py:stage_input/publish_artifacts`,
  `optimization.py:stage_research_input`, `final_staging.py:stage_final_input`,
  `portable_staging.py:_write_portable_inputs` (also called by portable final
  staging), `environment_evidence.py:publish_native_environment`, and
  `portable_optimization.py:publish_portable_environment/publish_portable_artifacts`.
- `runtimes/evaluator/runner.py` and `portable_runner.py` run output writers whose
  lifetime is not the DB transaction lifetime. Already registered evaluation IDs,
  containment evidence and confirmed-stop fencing must remain authoritative.
- Legacy `src/job.py:submit/run_job` logs and `src/data_cache.py:load_data` cache
  temporaries live outside the owned storage protocol and must remain out of
  scope or gain a separate explicit exclusion protocol. Never sweep `/tmp`, the
  repository `log` directory or arbitrary user-specified paths as a fallback.
- Cold restore writes a different, new, empty destination. Backup/restore and
  maintenance jobs must be excluded from a simultaneous cleanup of their target;
  do not treat operator processes as exempt writers.

### Required acceptance before apply is enabled

1. Real PostgreSQL races for shared writer vs exclusive maintenance locks,
   admissions/retries/uploads/bootstrap/publication, owner-lock ordering, timeout
   and lost-connection cases; include pending outbox and unknown-stop states.
2. Actual filesystem crash/race checks around create/write/fsync/link/rename/unlink
   and link-count changes. Pin directories and revalidate exact candidate inode
   identity, age and all reference protections immediately before unlink; bound
   operations and bytes. Reject symlinks/unknown names and stop on uncertainty.
3. A separately owned disposable diagnostic-log namespace with an explicit
   reference contract, or keep all current Run artifact logs permanently excluded.
4. Dry-run remains default; explicit operator apply uses a fresh inventory, never
   a stale JSON path list. Produce a private bounded receipt of performed/skipped
   actions and fsync the affected directories. Never auto-delete Run data/source/
   final results/checkpoints/packages or expand into general orphan GC.
5. Real retry/resume and backup/restore drills after the actual cleanup, plus
   fault-injected partial cleanup evidence. No mock/collection-only result can
   satisfy these gates. Only then mark the retention portion of V22/B7 complete.

## Validation supplied with this stage

`tests/operations/test_retention.py` uses actual temporary filesystem bytes and
fake database snapshots, with no subprocess/network/strategy execution. It covers
policy/age limits, all retained classes, unknown references, corruption, changed
snapshots, inode replacement, mutation during reads, symlinks/hard links/FIFOs,
resource bounds, CLI sanitization and apply refusal before inspection.

`tests/integration/test_retention_preview.py` is an opt-in real PostgreSQL case
using the existing cold-drill fixture. It seeds a genuine historical M3 database,
verifies retention preview against persisted artifact/data/checkpoint references,
compares all historical rows and bytes afterward, and rejects active Run and
unstopped-attempt states. It does not execute deletion or establish apply safety.
It requires the same hosted drill opt-in and maintenance credentials/client
prerequisites as `test_backup_restore.py`. Collection/skipping is not a pass.
