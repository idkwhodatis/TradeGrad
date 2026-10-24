"""Numeric-only observations of this job's authenticated disposable PostgreSQL.

Never emit logs, SQL errors, environment values, paths, row contents or Docker
configuration. This diagnostic is not used by production or Compose services.
"""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

CONTAINER = "tradegrad-ci-postgres"
PHASES = ("provisioned", "after-integration", "before-removal")
INSPECT = (
    '{{.Id}}|{{.Name}}|{{index .Config.Labels "io.tradegrad.ci"}}|'
    "{{.State.Status}}|{{.State.OOMKilled}}|{{.State.ExitCode}}"
)
SQL = """SELECT
    (SELECT coalesce(sum(size), 0)::bigint FROM pg_ls_waldir()),
    pg_database_size(current_database()),
    pg_size_bytes(current_setting('max_wal_size')),
    pg_size_bytes(current_setting('min_wal_size')),
    extract(epoch FROM current_setting('checkpoint_timeout')::interval)::bigint,
    (current_setting('fsync') = 'on')::int,
    (current_setting('full_page_writes') = 'on')::int,
    (current_setting('synchronous_commit') = 'on')::int
"""
PG_FIELDS = (
    "wal_bytes",
    "database_bytes",
    "max_wal_bytes",
    "min_wal_bytes",
    "checkpoint_timeout_seconds",
    "fsync_enabled",
    "full_page_writes_enabled",
    "synchronous_commit_enabled",
)


def numeric_fields(raw, count):
    if not re.fullmatch(r"[0-9]{1,20}(?:\|[0-9]{1,20})*\n?", raw):
        raise ValueError("Invalid bounded numeric observation")
    values = [int(value) for value in raw.strip().split("|")]
    if len(values) != count or any(value > 2**63 - 1 for value in values):
        raise ValueError("Invalid bounded numeric observation")
    return values


def collect(*, run=subprocess.run):
    def capture(argv):
        result = run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode or len(result.stdout) > 1024:
            raise ValueError("Observation unavailable")
        return result.stdout

    report = {
        "state": "unavailable",
        "oom_killed": None,
        "exit_code": None,
        "filesystem_total_bytes": None,
        "filesystem_used_bytes": None,
        "filesystem_available_bytes": None,
        **{field: None for field in PG_FIELDS},
    }
    try:
        identity = capture(["docker", "inspect", "--format", INSPECT, CONTAINER])
        match = re.fullmatch(
            r"([0-9a-f]{64})\|/tradegrad-ci-postgres\|postgres\|"
            r"(created|running|paused|restarting|removing|exited|dead)\|"
            r"(true|false)\|([0-9]{1,3})\n?",
            identity,
        )
        if match is None:
            return report
        container, state, killed, exit_code = match.groups()
        report.update(
            state=state, oom_killed=killed == "true", exit_code=int(exit_code)
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return report
    # Independent probes preserve the usable observation if either one fails.
    try:
        block, total, free, available = numeric_fields(
            capture(
                [
                    "docker",
                    "exec",
                    container,
                    "stat",
                    "-f",
                    "-c",
                    "%S|%b|%f|%a",
                    "/var/lib/postgresql",
                ]
            ),
            4,
        )
        if (
            block <= 0
            or not 0 <= available <= free <= total
            or block * total > 2**63 - 1
        ):
            raise ValueError("Invalid filesystem observation")
        report.update(
            filesystem_total_bytes=block * total,
            filesystem_used_bytes=block * (total - free),
            filesystem_available_bytes=block * available,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        values = numeric_fields(
            capture(
                [
                    "docker",
                    "exec",
                    container,
                    "psql",
                    "--no-psqlrc",
                    "--no-password",
                    "--tuples-only",
                    "--no-align",
                    "--set=ON_ERROR_STOP=1",
                    "--username=tradegrad_ci",
                    "--dbname=tradegrad_ci",
                    "--command",
                    SQL,
                ]
            ),
            len(PG_FIELDS),
        )
        if any(value not in (0, 1) for value in values[-3:]):
            raise ValueError("Invalid durability observation")
        report.update(zip(PG_FIELDS, values, strict=True))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return report


def expected_configuration(report):
    """Check actual server values, not just a source-level workflow assertion."""
    return (
        report["state"] == "running"
        and report["filesystem_total_bytes"] == 2 * 1024**3
        and report["max_wal_bytes"] == 256 * 1024**2
        and report["min_wal_bytes"] == 80 * 1024**2
        and report["checkpoint_timeout_seconds"] == 60
        and all(report[field] == 1 for field in PG_FIELDS[-3:])
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=PHASES, required=True)
    parser.add_argument("--require-config", action="store_true")
    args = parser.parse_args(argv)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("This diagnostic is only for an ephemeral GitHub Actions job")
    report = collect()
    body = {
        "format": "tradegrad.ci-postgres-resources.v1",
        "phase": args.phase,
        **report,
    }
    content = json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n"
    target = Path("var/ci-evidence")
    try:
        target.mkdir(parents=True, exist_ok=True)
        (target / f"postgres-resources-{args.phase}.json").write_text(content)
    except OSError:
        # Even artifact-volume exhaustion must not leak a raw path/traceback.
        print(json.dumps({**body, "evidence_status": "write_failed"}, sort_keys=True))
        return 1
    print(content, end="")
    return int(args.require_config and not expected_configuration(report))


if __name__ == "__main__":
    raise SystemExit(main())
