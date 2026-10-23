"""Hosted read-back of this disposable project's per-service logging options.

Never reads log contents, environment, mounts or daemon-managed log files.
Configuration evidence is not a claim that a rollover stress test was executed.
"""

import json
import os
import re
import time

from script.ci_compose_cleanup import command
from script.compose_runtime import validate_service_logging

SERVICES = {
    "database",
    "api",
    "research-dispatcher",
    "research-worker",
    "evaluator-reconciler",
    "data-dispatcher",
    "data-worker",
    "frontend",
}


def verify(source, call=command, clock=time.monotonic):
    project = source.get("COMPOSE_PROJECT_NAME", "")
    if source.get("GITHUB_ACTIONS") != "true" or not re.fullmatch(
        r"tg-ci-[0-9a-f]{16}", project
    ):
        raise ValueError("Only this disposable hosted project can be inspected")
    expected = validate_service_logging(source)
    deadline = clock() + 30
    raw = call(
        "ps",
        "--filter",
        "label=com.docker.compose.project=" + project,
        "--format",
        '{{.ID}} {{.Label "com.docker.compose.service"}}',
        deadline=deadline,
        max_stdout=8192,
    )
    seen = set()
    rows = raw.splitlines()
    if len(rows) != len(SERVICES):
        raise ValueError("Required running service inventory differs")
    for line in rows:
        parts = line.split()
        if len(parts) != 2:
            raise ValueError("Invalid scoped container inventory")
        identity, role = parts
        if (
            not re.fullmatch(r"[0-9a-f]{12,64}", identity)
            or role not in SERVICES
            or role in seen
        ):
            raise ValueError("Unknown or duplicate scoped service")
        seen.add(role)
        value = json.loads(
            call(
                "inspect",
                "--format",
                "{{json .HostConfig.LogConfig}}",
                identity,
                deadline=deadline,
                max_stdout=2048,
            )
        )
        if value != {"Type": "json-file", "Config": expected}:
            raise ValueError("Service logging differs from the bounded reviewed policy")
    return {
        "schema_version": "tradegrad.service-log-policy.v1",
        "services": len(seen),
        "driver": "json-file",
        "options": expected,
        "runtime_configuration_verified": True,
        "rollover_stress_test_executed": False,
        "immutable_run_logs_deleted": False,
    }


def main():
    try:
        print(json.dumps(verify(os.environ), sort_keys=True))
    except Exception:  # noqa: BLE001 -- never print raw daemon output or private paths
        raise SystemExit("Hosted service-log policy verification failed") from None


if __name__ == "__main__":
    main()
