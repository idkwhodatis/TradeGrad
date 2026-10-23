# V1 cold retention: cooperative exclusion and explicit apply

## Implementation status, not a release acceptance claim

The cold command now implements dry-run-first cleanup of exact expired atomic
publisher temporaries. It is no longer a preview-only substitute. The change is
prepared for real PostgreSQL concurrent-writer and disposable cleanup/recovery
acceptance; those hosted gates have **not run in this local stage**. Do not call
V22/B7 accepted or use this command on user storage until the exact integrated
head passes the acceptance checklist below. No production cleanup, migration,
service startup, credential change, scheduler, API cleanup endpoint or automatic
GC is introduced.

All existing persistent Run logs (`logs.txt`, `engine.log`, `policy.log`) remain
retained, including orphan logs. They are immutable execution evidence, not a
separately owned high-volume diagnostic namespace. `--log-days` reports aged
retained logs; it does not silently make them disposable. Docker runtime log
rotation stays separate. There is no new general orphan-GC or log namespace.

## Cold operator workflow

1. Stop API/admission, bootstrap, both dispatchers/reconcilers, queue workers,
   evaluators/model subprocesses and other storage writers. Resolve queued,
   active, leased, pending-outbox and unconfirmed-stop states using the existing
   lifecycle recovery procedure. Never falsify records to pass inspection.
2. Use the correct PostgreSQL database and its trusted, already provisioned local
   POSIX storage root. All writers must use this protocol/version and the same
   underlying directory inode. Stop migrations, raw database clients, legacy
   tools and external filesystems writers. Network/object stores, hostile writers,
   root/ancestor renames and multiple database installations sharing a root are
   outside this protocol. Have a fresh verified cold backup/restore drill.
3. Supply the database URL through a private environment variable, never a CLI
   argument or SQL log. The cold-backup TCP/schema/connection restrictions apply.
   The command takes both exclusive guards even for the default dry-run:

   ```sh
   python -m script.retention_v1 \
     --storage /srv/tradegrad/storage \
     --database-url-env RETENTION_DATABASE_URL \
     --acknowledge-quiesced --dry-run \
     --temporary-days 7 --log-days 30 \
     --max-files 100000 --max-total-bytes 20000000000 \
     --timeout-seconds 600
   ```

4. Review the private JSON report's exact candidate paths/count/bytes and
   `plan_sha256`. A plan is not a stale path list consumed for deletion. Apply
   constructs and verifies a new complete reference/inode/hash inventory under
   both exclusive guards and requires the same plan hash. Changing a reference,
   file, directory identity, eligibility or retention policy invalidates it.
5. After explicit approval, use the same policy/limits plus:

   ```sh
   --apply --plan-sha256 <reviewed-plan-sha256> \
   --acknowledge-apply delete-only-expired-unreferenced-atomic-temporaries \
   --receipt /srv/private-operations/retention-new-id.jsonl
   ```

   The receipt must be a new file outside the managed root, under an existing
   nonsymlink directory. It is created exclusively with mode 0600. No existing
   receipt is overwritten. Keep reports/receipts private: they contain relative
   storage identifiers, hashes, sizes and inode metadata, not source/log bodies,
   credential values or database URLs. Do not send the plan to another sweeper.
6. Exit 0 means the requested dry-run or apply completed. Only `status=applied`
   and its `deleted_files` count claim cleanup. Exit 2 means missing configuration
   or mandatory acknowledgement; exit 1 means failure; interruption returns 130.
   Failed/interrupted apply can be **partially completed**. It is not rollback.
   Inspect the private journal and storage, then create/review a new plan before
   any retry. CLI errors do not disclose exception contents.

Ages are whole days from 1 to 36500, measured against the newer of mtime/ctime.
Backdating mtime cannot expire a newly created/changed inode. Future timestamps
are retained. Exact age-boundary inclusion is defined by the cutoff. CLI time is
not caller-overridable. File/directory counts, file/total/manifest bytes, depth
(32), receipt bytes and elapsed scan/apply work are bounded. Syscalls are not
preempted; this is not a hard wall-time watchdog.

## Lock protocol and failure cases

- Global PG lock key is the two-int pair `0x54475244, 0x434f4c44` (`TGRD/COLD`).
  PostgreSQL's two-int advisory keyspace does not collide with existing hashed
  bigint owner/package keys. It is deliberately database-wide, not owner-scoped.
- `Session.after_begin` obtains `pg_try_advisory_xact_lock_shared` before the first
  statement of every PostgreSQL ORM transaction, including reads that may later
  mutate. It covers admission, quotas/settings, upload publication through commit,
  package publication, lifecycle fences, dispatch/recovery and reconciler writes.
  SAVEPOINTs reuse the outer lock. No optimistic continuation on denial exists.
- Each of the six production worker entrypoints retains an autocommit PG
  session-shared lock and a shared flock on the provisioned storage directory
  through the entire function, including heartbeat-independent writes, all
  failure/finally cleanup and existing confirmed-stop handling. Nested dispatch
  into a configured worker reuses the same lifecycle guard. Direct configured
  entrypoint calls acquire their own guard.
- Global PG is acquired before owner/row/queue locks and before storage locks.
  API transactions may take a publisher's root shared flock after owner locks;
  workers already hold it before child transactions. Maintenance takes only PG
  exclusive then root exclusive, never owner/row locks. Every barrier acquisition
  is a try-lock: contention refuses immediately and cannot form a wait cycle.
  Owner/row locks retain their existing bounded application behavior.
- Losing the lifecycle PG connection does not release the independent filesystem
  guard. Cleanup may then obtain PG exclusive but must refuse root exclusive.
  If the entire worker dies, its filesystem guard ends, but persisted unfinished
  attempt/evaluator/model-process admission records still prohibit cleanup.
  Neither expired leases nor elapsed time establish that a child stopped.
- Maintenance holds one read-only PG transaction and its exclusive lock through
  the final durable receipt, with every snapshot query on that same connection.
  Before each unlink it verifies the connection/transaction/backend identity.
  It never reconnects and continues deletion under an assumed replacement lock.
  Session lifecycle unlock uncertainty invalidates the pool connection.
- Shared root locks use the actual directory FD, not a replaceable lock file.
  Nested shared atomic publications are compatible. The command's exclusive
  root lock remains held while it revalidates descriptors and fsyncs receipts.
- API liveness does not query PG and stays available. Database-backed API reads,
  readiness and writes get the existing sanitized 503 response during maintenance.
  This is intentional cold downtime, not a hot-maintenance design. Startup and
  schema migration engines are not modified by the ORM hook.

### Coverage and explicit exceptions

The admission hook is installed by `core/db.py`, `services.py` and both dispatcher
modules, before business/queue-row locks. It covers all native, research, portable
and final submission/resume/claim/completion services, data submit/retry/claim/
fence/reconcile, Settings quota/account mutations and package publication without
inserting individual locks into overlapping service implementations.

`guarded_worker` wraps data `execute`, research `execute`, `execute_configured`,
`execute_portable_configured`, `execute_final`, `execute_portable_final`.
The guard therefore spans staging, checkpoint/model replay writes, environment
publication, candidate output, normalized dataset staging/rename, and their
cleanup. Existing external-evaluator registration/stop fences remain authoritative.
Bootstrap's direct `seed_fixtures` forces transaction admission before provisioning.

`publish_bytes(..., durable_root=...)` and `provision_directory(...,
durable_parent=...)` take a shared lock for their declared boundary. Every managed
production atomic publisher supplies the root. Legacy no-root publication is an
explicit offline helper, not an allowed way to write into maintained storage.
Standalone staging, candidate/model/checkpoint helpers do not create their own
PG lock; in production they execute only inside the guarded worker. No-root
legacy cache/log tools outside the owned namespaces are never cleanup targets.

Backup holds a shared root guard for its complete source inspection/copy/check.
Restore holds a shared root guard from new-root provisioning through marker,
copy, SQL restore, verification and final marker removal. An interrupted restore
marker is an unknown namespace and prohibits cleanup independently.

Raw Procrastinate internals and migration/admin SQL do **not** receive ORM locks.
The operator must stop them. Cleanup rejects every runnable `todo`/`doing` job,
both pending outboxes, nonterminal/leased imports, unfinished/unstopped attempts,
nonterminal/unstopped/incomplete evaluators, and unmatched/malformed model-process
admission/stop events. The new barrier does not claim to control arbitrary raw
SQL, another database, direct OS writes or old application versions.

## Deletion and durability contract

The locked inspection reuses the strict preview's namespace, reference and
manifest checks. It accepts only `datasets`, `runs`, `data-checkpoints`, `uploads`
and `strategy-packages`, rejects unknown schemas/namespaces, symlinks/hardlinks/
special files, verifies all known artifact/upload/data-block/package references,
dataset manifests and their data/source/preview members, and checkpoint manifests
and every declared checkpoint member. Referenced dataset/checkpoint subtrees are
preserved in full. Missing or corrupt references refuse the entire operation.

Only the existing exact atomic temporary layouts are eligible: hashed upload,
checkpoint and package temporaries; Run `.execution-environment.json` temporaries;
evaluation `.engine.log`/`.policy.log` temporaries. UUID owner/run/attempt/evaluation
components and 32-hex publisher suffixes are required. A direct DB reference or
protected subtree overrides eligibility. Generic `*.tmp`, arbitrary staging,
code/checkpoint/replay temporaries, published packages, immutable orphans and all
persistent logs are never candidates.

Dry-run and apply each compare two complete hash/inode inventories and two
reference snapshots. Plan identity includes every file/directory fingerprint,
reference and candidate, but not the wall-clock report creation timestamp.
Immediately before each unlink the command rechecks the lock, pinned root and
all ancestry, candidate device/inode/mode/link-count/size/mtime/ctime and hash.
It unlinks only that relative name through the pinned parent FD, fsyncs the parent,
updates only its own expected directory mutation, then records completion.

The append-only JSONL journal durably writes an intent before unlink and a
completion after directory fsync. Failure can leave an unresolved intent; neither
its absence from a completion list nor an exception proves that unlink did not
happen. At the end, every surviving file, manifest, directory and DB reference
is verified again before a durable `complete` result. Partial failure stops further
deletions and records `incomplete` when storage permits. No recursive deletion,
rename, chmod, database deletion or cleanup of directories occurs.

## Validation and remaining acceptance gates

Local inert checks cover lock SQL/namespace/order, nested root guards, fail-fast
contention, lifecycle loss handling, all six entrypoint decorators, explicit-root
publishers, model unknown-stop receipts, acknowledgement gates and the existing
preview/backup/bootstrap durability regressions. They are not PG or power-loss
acceptance. No actual apply, process-exit test, database/socket/container/browser,
provider SDK, strategy or wrapper execution was performed locally.

Prepared hosted tests:

- `tests/integration/test_cold_retention_barrier.py`: real shared/exclusive PG
  contention and release, owner-lock ordering, full worker guard between
  transactions, own-connection loss with root still locked, API/readiness refusal.
- `tests/integration/test_cold_retention_cleanup.py`: actual expired disposable
  fixture unlink, all immutable/history preservation, private journal order,
  partial-failure/new-plan recovery, changed candidates/references/hardlinks,
  pending/active/unknown-stop refusal, and real backup → empty restore → upgrade.
- Existing `test_retention_preview.py`, atomic publisher crash tests and the
  cumulative research/data retry/resume suites remain required on the integrated
  head. Collection/skipping is not a pass. No fake session is PG evidence.

Before accepting V22/B7, run the above on hosted PostgreSQL and the supported
local filesystem, plus complete retry/resume-after-cleanup on real data/research
worker flows and adversarial create/write/fsync/link/rename/unlink crash windows.
Validate the explicit exceptions/operator shutdown procedure, and only then use
apply on real installations. No broad deletion claim or waiver of the original
retention requirement is implied by this preparation.

## Combined main-tree preparation

Integrated after portable operator admission on 2026-10-23 UTC, preserving its
worker implementation and the provider executor. The combined backend,
operations and Compose-helper suite passed 1,221 tests, including four existing
owned filesystem-writer exit/retry cases. The 18 new PostgreSQL barrier/cleanup
cases collected successfully; they did not run locally. The hosted workflow now
explicitly includes cold cleanup/restore tests in its opted-in private backup
drill. Ruff and whitespace checks passed. Full actual worker resume after cold
cleanup is a separate required prepared/hosted proof, not inferred from these
counts; acceptance remains incomplete until that and the exact-head gates pass.

## Prepared real worker resume-after-cleanup gate

`tests/system/test_cold_retention_recovery.py` now prepares the remaining native
research boundary proof. It reuses `ResearchProcessHarness` and its synthetic
upload/frozen-model optimization, never SQL updates to manufacture progress.
The pinned owned worker is briefly stopped at a real generated-candidate Docker
evaluation: cold planning must refuse its live lifecycle lock, HTTP pause is
admitted, and the worker is continued to checkpoint and finish its actual queue
job. The test requires confirmed stopped attempts/evaluators, no model SDK
process admissions (the model is explicitly in-process frozen), and drained
research/data queues/outboxes. All owned API/producer/worker processes must then
be identity-safely stopped before the cold operator is called.

The test hashes the full private inventory and validates every reference and
manifest, adds exactly one exclusive-created abandoned atomic upload temporary,
and uses explicit future **test inspection time** to age it without changing
business/database timestamps. Real dry-run → exact plan acknowledgement → apply
must remove only that fixture and produce a private durable completion journal.
All original files and DB references must remain identical. The same owned
service commands restart, HTTP resume must bind generation 2 to the exact
checkpoint, and the real worker must execute only the missing validation window.
Completed evaluations, all original source/checkpoint/replay/log bytes, model
call fingerprints, candidate lineage, and downloadable artifact hashes persist.
The test requires a new complete terminal checkpoint and exactly paused/succeeded
terminal events, not merely an HTTP accepted response.

Run only on the authorized hosted disposable runtime, after integrating the
identity-safe `ProcessHarness.stop_processes` / `script.ci_owned_group` helper:

```sh
TRADEGRAD_REQUIRE_COLD_RECOVERY=1 \
TRADEGRAD_REQUIRE_RESEARCH_INTEGRATION=1 \
TEST_DATABASE_URL="$TEST_DATABASE_URL" \
API_PYTHON="$API_PYTHON" DATA_PYTHON="$DATA_PYTHON" \
RESEARCH_PYTHON="$RESEARCH_PYTHON" \
"$RESEARCH_PYTHON" -m pytest tests/system/test_cold_retention_recovery.py -q \
  --junitxml=v1-cold-research-resume-junit.xml
```

The inherited 240-second absolute budget covers the test body; identity-safe
fixture cleanup has its own bounded containment. No workflow change is included
in this preparation. `tests/operations/test_cold_recovery_contract.py` checks only
source-level opt-in, SQL read-only, owned signaling and operation-order contracts.
Collection or those inert checks do not establish PG, Docker, deletion or resume
acceptance. Actual execution remains required on the exact cumulative head.

Scope note: the current native worker stores evidence under
`runs/{run}/attempts/{attempt}`, while the narrow retention temp/log classifiers
recognize `runs/{run}/{attempt}`. Unrecognized native paths remain preserved;
this gate deliberately uses the actual supported upload temporary layout and
checks native log bytes directly, rather than relying on the aged-log counter.
Expanding path eligibility/counting is not part of this test-only patch.
Discardable Docker operational log rotation remains separate from permanently
retained artifact/Run logs. Data-worker retry and the independent crash/partial
cleanup/backup-restore gates also remain required; this native resume case does
not substitute for them.
