"""CI-only streaming PostgreSQL clients from the job's recorded service image.

No host bind mounts, connection URLs, command-line secrets or dump files are
created by this adapter. Backup tooling owns the bounded private output/input.
"""

import os
import re
import subprocess
import sys

TOOLS = frozenset({"pg_dump", "pg_restore"})
PG_FIELDS = (
    "PGHOST",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
    "PGSSLMODE",
    "PGCONNECT_TIMEOUT",
    "PGPASSFILE",
    "PGOPTIONS",
)


def command(tool, arguments, environment):
    """Return a constrained argv; never return environment values in argv."""
    container = environment.get("TRADEGRAD_CI_POSTGRES_ID", "")
    if (
        environment.get("GITHUB_ACTIONS") != "true"
        or tool not in TOOLS
        or not re.fullmatch(r"[0-9a-f]{64}", container)
    ):
        raise ValueError("Only the recorded disposable CI database is supported")
    common = {"--no-owner", "--no-privileges", "--no-tablespaces", "--no-password"}
    allowed = common | (
        {"--format=custom"}
        if tool == "pg_dump"
        else {
            "--exit-on-error",
            "--single-transaction",
            "--dbname=" + environment.get("PGDATABASE", ""),
        }
    )
    if arguments != ["--version"] and (
        len(arguments) > 30 or any(item not in allowed for item in arguments)
    ):
        raise ValueError("Unsupported streaming PostgreSQL client arguments")
    if (
        environment.get("PGPASSFILE", "/dev/null") != "/dev/null"
        or environment.get(
            "PGOPTIONS", "-c statement_timeout=120000 -c search_path=public"
        )
        != "-c statement_timeout=120000 -c search_path=public"
    ):
        raise ValueError("Unsupported PostgreSQL client configuration")
    if arguments != ["--version"] and (
        environment.get("PGHOST") != "127.0.0.1"
        or environment.get("PGPORT", "5432") != "5432"
        or environment.get("PGUSER") != "tradegrad_ci"
        or not re.fullmatch(
            r"tradegrad_backup_test_[0-9a-f]{32}",
            environment.get("PGDATABASE", ""),
        )
        or not environment.get("PGPASSWORD")
    ):
        raise ValueError("Only generated backup-drill databases are supported")
    args = ["docker", "exec", "-i"]
    for name in PG_FIELDS:
        if name in environment:
            args.extend(["--env", name])
    return [*args, container, tool, *arguments]


def main(arguments=None, *, environment=None, run=subprocess.run, execute=os.execvpe):
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    environment = dict(os.environ if environment is None else environment)
    try:
        if not arguments:
            raise ValueError("Missing PostgreSQL client")
        args = command(arguments[0], arguments[1:], environment)
        container = environment["TRADEGRAD_CI_POSTGRES_ID"]
        check = run(
            [
                "docker",
                "inspect",
                "--format",
                '{{.Name}} {{index .Config.Labels "io.tradegrad.ci"}}',
                container,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            env=environment,
        )
        if check.returncode or check.stdout != b"/tradegrad-ci-postgres postgres\n":
            raise ValueError("Recorded CI database identity differs")
        # Archive bytes stream directly. The caller supplies bounded, private
        # stdio and a timeout; never buffer or print the archive here.
        # Replace this shim so caller timeouts target the actual docker client,
        # not a wrapper that could leave an extra child behind. The backup tool
        # already owns a private stderr sink and bounded archive stdio.
        return execute(args[0], args, environment)
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Disposable PostgreSQL client failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
