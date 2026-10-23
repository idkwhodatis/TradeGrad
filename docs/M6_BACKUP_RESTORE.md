# M6 private cold backup and restore drill

Status: local preparation, not production recovery certification. The pure tests
use actual filesystem bytes and injected client/database boundaries. The real
PostgreSQL drill is opt-in and must pass on the hosted PostgreSQL service before
claiming database recovery or migration proof. Collection is not execution. This
document does not close the other security, cancellation, browser, performance or
runtime gates in plan sections 10.2/10.3.

## Supported profile and trust boundary

`python -m script.backup_v1` implements one deliberately bounded profile:

- A trusted, single-host Linux operator has stopped the API, all research/data
  workers, dispatchers, reconcilers, scheduled producers and every other database
  or filesystem writer. Source and target namespaces remain under exclusive
  operator control throughout the operation. The tool cannot stop these services
  or independently prove that no external writer exists.
- Source is a dedicated PostgreSQL application database, with one actual Alembic
  revision, `003_m3` or `004_m5`, and application objects in `public`. Extra user
  schemas, large objects, additional extensions, foreign-data access, replication
  objects and event triggers are rejected. A non-public schema needs a separately
  implemented and tested profile, not an unverified flag.
- Official `pg_dump` custom format covers the complete logical database, not an
  invented SQL subset. Both clients must report the same major version as the
  source/target server. Restore requires that same PostgreSQL major, exact
  recorded application reference, and explicit operator trust in the archive SQL.
- A local filesystem honors regular-file and directory fsync. The operator names
  an existing, already durable parent; its ancestry must remain stable. Existing
  durable-root primitives provision each descendant explicitly. This is an fsync
  protocol, not power-loss or filesystem/device certification.
- The source storage root contains only `datasets`, `runs`, `data-checkpoints`,
  `uploads` and `strategy-packages`. Every regular file and empty directory in
  these namespaces is included, not only a public export or summary. That retains
  normalized/source data, previews, strategy/result artifacts, checkpoint state,
  retained upload bytes and private package archives. File bytes are SHA-256
  checked; dataset, artifact, block, checkpoint and package DB references are
  cross-checked against their corresponding file inventories.

Backups are **private, sensitive operator artifacts**. Full database records
include account password hashes, user identities, configurations, strategy code,
model/checkpoint state, logs and other private application content; market-data
files may have separate licensing restrictions. The tool does not scrub secrets
that someone previously embedded inside arbitrary application records or code.
Do not upload a dump, backup directory or test temporary directory to GitHub/CI
artifacts, attach it to a bug report, publish it, or expose it through the WebUI.
Use operator-approved encrypted private storage and retention controls outside
this tool; encryption/key management is not implemented here.

Deployment environment files, API/provider secrets, JWT signing keys, PostgreSQL
connection URLs/passwords, cluster roles, ownership/ACL grants, tablespaces and
runner process-handle directories are not selected as backup inputs. Keep
`TRADEGRAD_RUNNER_STATE_DIR` outside the application storage namespaces. Historical
sandbox IDs and stopped-attempt audit rows remain in the DB; they are not live
handles and must never be used to reattach an old host/container process. Provision
fresh runtime secrets and a fresh runner-state directory separately through the
normal authorized deployment process. The command never creates credentials,
changes permissions in a database, starts execution, creates/drops a database,
uses `--clean`/`--create`, or resumes services.

A hash proves bytes have not changed relative to the supplied manifest. It does
**not** prove who made either file. A malicious party can replace both. PostgreSQL
archive restoration can execute code chosen by the source database owner; trust
its provenance independently before acknowledging `--trust-backup-sql`.
See the official [pg_restore safety warning](https://www.postgresql.org/docs/18/app-pgrestore.html)
and [pg_dump logical-backup scope](https://www.postgresql.org/docs/18/app-pgdump.html).

## Stop and inspect before creating a backup

1. Block new submissions/imports and API mutations. Use the normal application
   cancellation/pause controls while workers can still complete the stop
   protocol. Wait for all child sandboxes to be confirmed stopped; an expired
   lease or a dead coordinator alone is not stop confirmation.
2. Drain or resolve pending queue deliveries and imports, then stop the API,
   workers, dispatchers, reconcilers and every remaining writer. Do not manually
   rewrite status/stop flags to bypass the checks. The tool refuses queued or
   active runs, unfinished/unstopped attempts, registered/unstopped evaluations,
   nonterminal or leased imports, and `todo`/`doing` queue jobs. A safely stopped
   paused run is allowed and retains its immutable checkpoint. Preserved bytes
   do not guarantee resumability: resume must still refuse when the exact bound
   runtime image IDs or compatible execution environment cannot be reproduced
   after restore. Never rewrite image bindings to make a resume pass.
3. Confirm the storage root is the complete private application store, not a
   selection of exported artifacts, and that runner state/config/credentials are
   elsewhere. Select a new, private backup path below an existing durable parent.
   Its immediate parent must already exist. Source and destination must be
   disjoint; symlinks in roots/ancestors are rejected.
4. Record the exact deployed application's lowercase Git SHA, not a moving tag.
   This is an operator-supplied release identity. The command does not fetch,
   verify signatures of, or rebuild that release. Keep its source, dependency
   locks and required image digests available separately; image references
   already bound in DB/storage records are retained.

Provide the database URL only through an existing private environment variable.
It must explicitly contain one TCP host, one simple-identifier username and the
simple-identifier database name. An omitted port is normalized to 5432 for both
inspection and client tools. Unix-socket, multi-host, service, hostaddr and other
connection-string overrides are not supported; only one `sslmode` URL query
option is accepted, defaulting consistently to `prefer`. Inspector and client
password-file lookup are both explicitly disabled with `/dev/null`.

Run in a clean operator process without ambient libpq connection variables.
Recognized settings are derived from installed libpq metadata, including empty
settings, with service-file controls also rejected. This is checked immediately
before inspection and when creating the client environment, so `PGHOST`,
`PGPORT`, `PGUSER`, `PGDATABASE`, `PGSERVICE`, `PGHOSTADDR`, `PGOPTIONS`, SSL and
authentication overrides cannot silently select a different inspected/restored
target. The tool does not change or unset operator environment settings for you.
`DATABASE_URL`/`TARGET_DATABASE_URL` and the two executable override variables are
not ambient libpq settings and remain supported.
Do not paste a connection URL into a CLI argument or enable shell tracing. No example credential
is installed, and these commands do not provision a database or a deployment.
The paths below are illustrative; the operator must choose their own existing,
trusted durable directories.

```sh
# DATABASE_URL already names the stopped source database.
# SOURCE_APPLICATION_REF is the exact recorded deployed Git SHA.
python -m script.backup_v1 create \
  --backup /private/backups/new-snapshot \
  --storage /private/tradegrad/storage \
  --durable-parent /private/backups \
  --application-ref "$SOURCE_APPLICATION_REF" \
  --acknowledge-quiesced

python -m script.backup_v1 verify \
  --backup /private/backups/new-snapshot \
  --application-ref "$SOURCE_APPLICATION_REF"
```

A complete backup contains `database.dump`, `storage/`, `manifest.json` and a
`COMPLETE` marker binding the exact manifest bytes. The manifest records format,
actual schema revision, application SHA, PostgreSQL major, UTC creation time,
all relative file sizes/hashes and storage directories. It contains no database
connection configuration. The source inventory and DB references are rechecked
before completion to detect ordinary accidental changes; this is not hot-backup
consistency or a defense against concurrent hostile namespace writers.

Do not accept merely seeing the `COMPLETE` filename: require successful command
completion and `verify` against the whole directory. Unexpected files/directories,
duplicate paths/JSON fields, traversal, symlinks, hard links, devices, sockets,
FIFOs, mismatched hashes/sizes and unsupported versions fail closed. A partial
creation is never reused or silently overwritten. Keep it private for inspection
and choose a new destination for any retry.

## Restore into new empty targets only

The operator must first provision a dedicated **empty** PostgreSQL database with
an empty standard `public` schema (including no collations, conversions,
operators/opclasses/opfamilies, text-search objects or statistics), and no
database-local default-ACL records, including global defaults with namespace 0.
Use a newly provisioned standard dedicated database; administrator-owned schemas
and catalogs must remain quiescent through restore. Provision the target using
the normal deployment process. Do not run Alembic before restore, select a database containing application objects, or
select the source database. This utility inspects the target and never drops or
clobbers existing data. The restore storage path must not exist, even as an empty
or previously interrupted directory. The backup and restored storage trees must
be disjoint. Keep all target services stopped and all target namespaces exclusive.

```sh
# TARGET_DATABASE_URL already names the new EMPTY target, not the source.
python -m script.backup_v1 restore \
  --backup /private/backups/new-snapshot \
  --storage /private/recovery/new-storage \
  --durable-parent /private/recovery \
  --database-url-env TARGET_DATABASE_URL \
  --application-ref "$SOURCE_APPLICATION_REF" \
  --acknowledge-quiesced \
  --trust-backup-sql
```

Restore verifies **all** declared bytes and exact inventory before creating its
storage target. It copies immutable files with no-clobber writes, rechecks the
backup and target DB, and verifies a private, unlinked archive spool before
feeding that exact stream to `pg_restore`. PostgreSQL uses `--single-transaction`
and `--exit-on-error`; a nonzero exit, timeout, copy/hash/fsync failure or
interruption is never success. The client stdout is bounded, stderr is discarded
rather than risking disclosure, and the CLI prints only generic failure text.
Connection secrets are in the child libpq environment, not command arguments.
The restore command's only database argument is a strict plain database name.

On abnormal client completion, cleanup signals only its recorded process group
before reaping the leader. Non-reaping Linux exit observation retains the owned
identity even when a wrapper exits before a descendant closes stdout. Bounded
post-reap probes require the group to disappear; uncertainty keeps restoration
incomplete. Local process termination does not prove a remote PostgreSQL command
rolled back, which is why failed targets are never automatically promoted.

`.restore-incomplete` remains after failure, except that a failure flushing the
final marker removal can leave its visibility uncertain. Successful exit is
mandatory, not marker absence alone. If failure occurs after SQL commit or a
wrapper is interrupted, the target DB may already contain restored objects.
The tool cannot atomically commit a database and filesystem together and never
claims that it can. Do not start services, retry into that target, or delete the
original backup. Inspect privately and retry only into separately provisioned
empty database/new storage targets. Cleanup is an explicit operator action.

On success, the tool checks the restored schema version, immutable DB/file
references and every restored byte before removing the incomplete marker.
Then, while services remain stopped:

- Confirm the recorded application release, schema revision, source data hashes
  and representative run/artifact/checkpoint retrieval using the normal operator
  checks. Preserve the original backup unchanged.
- If upgrading, first restore the historical version, then apply the real
  Alembic migration chain with the target release. Do not stamp a version, rebuild
  old rows through the newest ORM, downgrade away immutable evidence, or assume
  config JSON bytes/hashes may change.
- Provision authorized fresh runtime secrets and runner state separately. Ensure
  old hosts/containers cannot write to the restored store. Start the correct
  deployment topology only after its runtime/security gates and recovery checks
  pass. This CLI does not start workers or run user strategy/policy code.

## Resource limits and client wrappers

Defaults: 100,000 storage files, at most 200,016 storage directories, 2 GB per
storage file, 10 GB custom archive, 20 GB combined archive+storage bytes, 32 MB
JSON manifest, and a 600-second timeout per dump/restore client. SQL inspection
has a 10-second statement timeout; client connections use a 5-second connection
timeout and 120-second statement timeout. Client version reads have a 10-second
limit. These are safety ceilings, not measured capacity promises or RPO/RTO.
The restore needs extra scratch disk up to the archive size for the private
verified spool, in addition to PostgreSQL and restored storage space.

Each numeric limit has an explicit CLI option (`--max-files`,
`--max-file-bytes`, `--max-total-bytes`, `--max-dump-bytes`,
`--max-manifest-bytes`, `--timeout-seconds`). Larger deployments need separately
measured limits and a tested operator profile, not silently unbounded allocation.

`PG_DUMP_EXECUTABLE` and `PG_RESTORE_EXECUTABLE` or their CLI equivalents select
trusted executable paths, not shell commands. Internal `PgTools` accepts fixed
argv tuples and an injectable bounded runner for pure tests. The clients stream
archive stdout/stdin; no host `--file` mount is needed. A CI-only wrapper can
execute the clients inside its independently verified, job-owned PostgreSQL
container. It must preserve `--version`, verify container ownership, permit only
the expected flags/libpq variables and never disclose its stream or credentials.
The application wrapper does not inherit arbitrary ambient PG service files or
application secrets; `PGPASSFILE=/dev/null` disables ambient password-file use. Only the effective
operator `HOME` is also preserved for native clients so default SSL certificate
path lookup matches native SQLAlchemy/libpq inspection. Its value never enters
manifest fields, reports or command arguments; arbitrary SSL/provider/cloud
environment settings are not forwarded. Custom TLS/authentication deployments
need separate real evidence. The CI wrapper is a separate localhost non-TLS
service profile with its own container filesystem; that fixture does not prove
custom TLS or external authentication deployments.

## Mandatory opt-in hosted drill

`tests/integration/test_backup_restore.py` is the real recovery/upgrade drill.
It needs `RUN_BACKUP_RESTORE_DRILL=1`, `TEST_DATABASE_URL` for a dedicated ephemeral
PostgreSQL service, and matching-major `pg_dump`/`pg_restore` clients (or the
narrow trusted CI wrappers). The test role needs `CREATEDB`. An absent opt-in
skips explicitly; when enabled, missing URL/tools, wrong database type, connection
errors, missing privileges and any dump/restore/migration error fail.

```sh
RUN_BACKUP_RESTORE_DRILL=1 python -m pytest \
  tests/integration/test_backup_restore.py -q -ra \
  --junitxml=backup-restore-junit.xml
```

The test creates only two generated `tradegrad_backup_test_<32hex>` databases on
the configured test service. It applies actual Alembic `003_m3` from scratch,
seeds historical rows and genuine file references, runs actual streamed custom
backup/restore, verifies all old table projections, exact config text/request
hashes and immutable bytes, then upgrades the restored database to `004_m5` and
checks old rows/config bytes remain unchanged. It does not use SQLite, mocks,
`metadata.create_all`, a downgrade disguised as an old database, or Alembic stamp.
Teardown drops only identifiers whose CREATE was confirmed; it never uses FORCE
or terminates other sessions. Failed cleanup is a reported failure, not a reason
to broaden deletion scope.

Only the deliberately non-sensitive JUnit/status report is eligible for CI
publication. Never upload the database dumps, pytest temporary directories,
source/restored storage, environment dump, or full command diagnostics. The
fixture uses synthetic bytes, but the backup feature's private-artifact policy
must also hold when the tooling is reused with real data.

## Explicit non-goals

No hot DB/filesystem snapshot, WAL/PITR, replica/cluster/global-role recovery,
automatic cleanup, signed/encrypted archive format, remote object storage,
network filesystem durability, power-cut test, cross-major PostgreSQL upgrade,
unknown schema migration, multi-host failover, hostile-writer defense, recovery
of live process handles, provider availability, optimizer determinism, broker
connectivity or authorization to operate a real deployment is claimed. These
require separate designs and evidence. This bounded drill covers the plan's
cold database+artifact recovery and historical upgrade slice only.
