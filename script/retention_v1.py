"""Operator-only locked cold retention. Dry-run first; explicit narrow apply."""

import argparse
import json
import os
import sys
from pathlib import Path

from backend.app.core.maintenance import MaintenanceBusy
from backend.app.operations_backup import BackupLimits, PostgresDatabase
from backend.app.operations_cold_retention import ACKNOWLEDGEMENT, cold_retention
from backend.app.operations_retention import RetentionError, RetentionPolicy


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
        help="Delete only freshly verified candidates matching a prior dry-run plan",
    )
    parser.add_argument(
        "--plan-sha256", help="Exact hash returned by a completed dry-run"
    )
    parser.add_argument("--acknowledge-apply", choices=[ACKNOWLEDGEMENT])
    parser.add_argument(
        "--receipt", type=Path, help="New private JSONL receipt outside managed storage"
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
    if args.apply and (
        not args.plan_sha256
        or args.acknowledge_apply != ACKNOWLEDGEMENT
        or args.receipt is None
    ):
        print(
            "Apply requires --plan-sha256, --acknowledge-apply and --receipt. Nothing was deleted.",
            file=sys.stderr,
        )
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
        result = cold_retention(
            database=PostgresDatabase(value),
            storage_root=args.storage,
            policy=policy,
            limits=limits,
            quiesced=True,
            apply=args.apply,
            expected_plan_sha256=args.plan_sha256,
            acknowledgement=args.acknowledge_apply,
            receipt_path=args.receipt,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (MaintenanceBusy, RetentionError) as exc:
        # These application errors contain only fixed safe operator messages.
        print(str(exc), file=sys.stderr)
        if args.apply:
            print(
                "Apply may be partial; inspect the private receipt. No rollback is implied.",
                file=sys.stderr,
            )
        return 1
    except KeyboardInterrupt:
        print(
            "Cold operation interrupted; inspect the private receipt if apply was requested. No rollback is implied.",
            file=sys.stderr,
        )
        return 130
    except Exception:  # noqa: BLE001 -- database/path exceptions must not reveal private data
        print(
            "Cold operation failed; inspect the private receipt if apply was requested. No rollback is implied.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
