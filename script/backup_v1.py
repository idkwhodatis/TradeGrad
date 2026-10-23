"""Private operator-only cold backup, verify and EMPTY-target restore command."""

import argparse
import os
import sys
from pathlib import Path

from backend.app.operations_backup import (
    BackupError,
    BackupLimits,
    PgTools,
    PostgresDatabase,
    create_backup,
    restore_backup,
    verify_backup,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("create", "verify", "restore"))
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--storage", type=Path)
    parser.add_argument("--durable-parent", type=Path)
    parser.add_argument(
        "--application-ref", required=True, help="Exact release Git SHA, not a tag/URL"
    )
    parser.add_argument(
        "--database-url-env",
        default="DATABASE_URL",
        help="Environment variable name only; never put its value on the command line",
    )
    parser.add_argument(
        "--acknowledge-quiesced",
        action="store_true",
        help="I stopped API, every worker/dispatcher/reconciler, imports, and all other writers",
    )
    parser.add_argument(
        "--trust-backup-sql",
        action="store_true",
        help="I trust the source archive; restore executes its SQL. Hashes are not authentication",
    )
    parser.add_argument(
        "--pg-dump-executable", default=os.environ.get("PG_DUMP_EXECUTABLE", "pg_dump")
    )
    parser.add_argument(
        "--pg-restore-executable",
        default=os.environ.get("PG_RESTORE_EXECUTABLE", "pg_restore"),
    )
    defaults = BackupLimits()
    for name in vars(defaults):
        parser.add_argument(
            "--" + name.replace("_", "-"), type=int, default=getattr(defaults, name)
        )
    args = parser.parse_args(argv)
    try:
        limits = BackupLimits(**{name: getattr(args, name) for name in vars(defaults)})
        if args.operation == "verify":
            verify_backup(
                args.backup, application_ref=args.application_ref, limits=limits
            )
            print(
                "Backup integrity verified. This does not establish authenticity or restore readiness."
            )
            return 0
        if args.storage is None or args.durable_parent is None:
            raise BackupError(
                "Storage path and an explicit trusted durable parent are required"
            )
        value = os.environ.get(args.database_url_env)
        if not value:
            raise BackupError("Database connection environment variable is unset")
        database = PostgresDatabase(value)
        tools = PgTools(
            pg_dump=(args.pg_dump_executable,), pg_restore=(args.pg_restore_executable,)
        )
        common = {
            "database": database,
            "storage_root": args.storage,
            "durable_parent": args.durable_parent,
            "application_ref": args.application_ref,
            "quiesced": args.acknowledge_quiesced,
            "tools": tools,
            "limits": limits,
        }
        if args.operation == "create":
            create_backup(destination=args.backup, **common)
            print(
                "Private backup created and integrity verified. Do not upload application backups to CI artifacts."
            )
        else:
            restore_backup(
                backup_root=args.backup, trust_backup=args.trust_backup_sql, **common
            )
            print(
                "Database and immutable bytes restored and verified. Keep services stopped until the operator restart checks pass."
            )
        return 0
    except KeyboardInterrupt:
        print(
            "Operation interrupted. Do not start services or reuse a partial destination.",
            file=sys.stderr,
        )
        return 130
    except Exception:  # noqa: BLE001 -- no connection/driver/file exception may reveal a credential
        print(
            "Operation failed; no complete result is claimed. Keep services stopped; preserve any partial target for private inspection and retry only into new empty targets.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
