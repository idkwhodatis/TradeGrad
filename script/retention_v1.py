"""Operator-only cold retention preview. Apply is deliberately unsupported."""

import argparse
import json
import os
import sys
from pathlib import Path

from backend.app.operations_backup import BackupLimits, PostgresDatabase
from backend.app.operations_retention import (
    APPLY_BLOCKER,
    RetentionPolicy,
    preview_retention,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage", required=True, type=Path)
    parser.add_argument(
        "--database-url-env",
        default="DATABASE_URL",
        help="Environment variable name, never its secret value",
    )
    parser.add_argument(
        "--acknowledge-quiesced",
        action="store_true",
        help="I stopped API, workers, dispatchers, imports, evaluators and all other writers",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="Default; advisory metadata only"
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Unsupported; fails before opening storage or the database",
    )
    parser.add_argument("--temporary-days", type=int, default=7)
    parser.add_argument(
        "--log-days",
        type=int,
        default=30,
        help="Age threshold for retained log counts, not deletion",
    )
    defaults = BackupLimits()
    for name in (
        "max_files",
        "max_file_bytes",
        "max_total_bytes",
        "max_manifest_bytes",
        "timeout_seconds",
    ):
        parser.add_argument(
            "--" + name.replace("_", "-"), type=int, default=getattr(defaults, name)
        )
    args = parser.parse_args(argv)
    if args.apply:
        print(APPLY_BLOCKER, file=sys.stderr)
        return 2
    try:
        policy = RetentionPolicy(
            temporary_days=args.temporary_days, log_days=args.log_days
        )
        limits = BackupLimits(
            **{
                name: getattr(args, name)
                for name in (
                    "max_files",
                    "max_file_bytes",
                    "max_total_bytes",
                    "max_manifest_bytes",
                    "timeout_seconds",
                )
            }
        )
        if not args.acknowledge_quiesced:
            print(
                "Stop every writer and acknowledge quiescence before previewing.",
                file=sys.stderr,
            )
            return 2
        value = os.environ.get(args.database_url_env)
        if not value:
            print("Database connection environment variable is unset.", file=sys.stderr)
            return 2
        result = preview_retention(
            database=PostgresDatabase(value),
            storage_root=args.storage,
            policy=policy,
            limits=limits,
            quiesced=True,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        print("Preview interrupted. No deletion was attempted.", file=sys.stderr)
        return 130
    except Exception:  # noqa: BLE001 -- database/path exceptions must not reveal private data
        print(
            "Cold inspection failed; no complete preview. No deletion was attempted.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
