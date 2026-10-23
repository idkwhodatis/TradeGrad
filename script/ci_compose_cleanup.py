"""Bounded cleanup of this disposable Compose project and its physical sandboxes.

Hosted CI only. Every query/output/container count is bounded; absence is checked
again after removal and after Compose teardown. No uncertain state is success.
"""

import json
import os
import re
import time
from pathlib import Path

from script.compose_bounds import run_bounded

TOTAL_SECONDS = 180
MAX_CONTAINERS = 256
MAX_INSPECT_BYTES = 262144
MANAGED = ("evaluator-v1", "portable-evaluator-v1")


def command(*arguments, deadline, max_stdout=MAX_INSPECT_BYTES):
    return run_bounded(
        [
            "docker",
            "--host",
            "unix://" + os.environ["TRADEGRAD_DOCKER_SOCKET"],
            *arguments,
        ],
        deadline=deadline,
        max_stdout=max_stdout,
    )


def container_ids(raw):
    ids = raw.split()
    if (
        len(ids) > MAX_CONTAINERS
        or len(set(ids)) != len(ids)
        or any(not re.fullmatch(r"[0-9a-f]{12,64}", value) for value in ids)
    ):
        raise ValueError("Malformed or oversized daemon container listing")
    return ids


def owned_containers(storage, deadline):
    owned = []
    count = 0
    for managed in MANAGED:
        ids = container_ids(
            command(
                "ps",
                "-aq",
                "--filter",
                f"label=io.tradegrad.managed={managed}",
                deadline=deadline,
                max_stdout=65536,
            )
        )
        count += len(ids)
        if count > MAX_CONTAINERS:
            raise ValueError("Combined container discovery bound exceeded")
        for container in ids:
            rows = json.loads(command("inspect", container, deadline=deadline))
            if not isinstance(rows, list) or len(rows) != 1:
                raise ValueError("Uncertain daemon object identity")
            value = rows[0]
            identity = value.get("Id", "")
            if not re.fullmatch(r"[0-9a-f]{64}", identity) or not identity.startswith(
                container
            ):
                raise ValueError("Inspected daemon object identity changed")
            labels = value.get("Config", {}).get("Labels", {})
            if (
                not isinstance(labels, dict)
                or labels.get("io.tradegrad.managed") != managed
            ):
                raise ValueError("Inspected daemon object labels changed")
            mounts = value.get("Mounts")
            if not isinstance(mounts, list) or len(mounts) > 16:
                raise ValueError("Uncertain or oversized mount inventory")
            for mount in mounts:
                source = mount.get("Source")
                if not isinstance(source, str) or len(source) > 4096:
                    raise ValueError("Uncertain daemon mount identity")
                path = Path(source)
                if (
                    mount.get("Type") == "bind"
                    and path.is_absolute()
                    and ".." not in path.parts
                    and path.is_relative_to(storage)
                ):
                    owned.append(identity)
                    break
    return owned


def main():
    project = os.environ.get("COMPOSE_PROJECT_NAME", "")
    if os.environ.get("GITHUB_ACTIONS") != "true" or not re.fullmatch(
        r"tg-ci-[0-9a-f]{16}", project
    ):
        raise SystemExit("Cleanup only accepts this helper's disposable hosted project")
    root = Path(os.environ["TRADEGRAD_STATE_ROOT"])
    expected = (
        Path(os.environ["GITHUB_WORKSPACE"])
        / "var"
        / ("compose-" + project.removeprefix("tg-ci-"))
    )
    if root != expected or any(path.is_symlink() for path in (root, *root.parents)):
        raise SystemExit("Disposable project and state identities do not match")
    deadline = time.monotonic() + TOTAL_SECONDS
    command(
        "compose",
        "-f",
        "infra/compose.full.yml",
        "stop",
        "--timeout",
        "45",
        deadline=deadline,
    )
    storage = root / "storage"
    for identity in owned_containers(storage, deadline):
        command("rm", "--force", identity, deadline=deadline, max_stdout=65536)
    # Successful rm/stop replies are not substituted for an absence observation.
    if owned_containers(storage, deadline):
        raise RuntimeError("Owned evaluator absence not confirmed after removal")
    command(
        "compose",
        "-f",
        "infra/compose.full.yml",
        "down",
        "--volumes",
        "--remove-orphans",
        deadline=deadline,
    )
    if owned_containers(storage, deadline):
        raise RuntimeError("Owned evaluator absence not confirmed after teardown")
    remaining = container_ids(
        command(
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={project}",
            deadline=deadline,
            max_stdout=65536,
        )
    )
    if remaining:
        raise RuntimeError("Compose project container absence not confirmed")
    print("Disposable project/evaluator absence verified after bounded cleanup")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - never expose private subprocess/config data
        raise SystemExit(
            "Disposable Compose cleanup failed; containment is not confirmed"
        ) from None
