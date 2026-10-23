"""Fail-closed single-host Compose provisioning, role startup and health checks.

Host prepare never contacts Docker. The operator owns the durable parent and
existing daemon access. No chmod/chown of arbitrary host trees or root fallback.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

ROLES = {
    "api": ["uvicorn", "backend.app.asgi:app", "--host", "0.0.0.0", "--port", "8000"],
    "research-worker": ["python", "-m", "workers.research", "worker"],
    "research-dispatcher": ["python", "-m", "workers.research", "dispatcher"],
    "evaluator-reconciler": ["python", "-m", "workers.research", "reconciler"],
    "data-worker": ["python", "-m", "workers.data", "worker"],
    "data-dispatcher": ["python", "-m", "workers.data", "dispatcher"],
}
SUPERVISORS = {"research-worker", "evaluator-reconciler"}
HEARTBEATS = {"research", "dispatcher", "reconciler", "data", "data_dispatcher"}


def state_root(environ=os.environ) -> Path:
    raw = environ["TRADEGRAD_STATE_ROOT"]
    root = Path(raw)
    if (
        not re.fullmatch(r"/[A-Za-z0-9_./-]+", raw)
        or ".." in root.parts
        or root == Path("/")
        or str(root) != raw
        or any(path.is_symlink() for path in (root, *root.parents))
    ):
        raise ValueError("Require a canonical absolute non-symlink state path")
    return root


def validate_owner(path: Path) -> None:
    value = path.stat()
    if (
        path.is_symlink()
        or not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.getuid()
        or value.st_gid != os.getgid()
        or stat.S_IMODE(value.st_mode) != 0o700
    ):
        raise ValueError(
            "State directories must be private and owned by the configured UID/GID"
        )


def validate_identity(environ=os.environ) -> None:
    if (
        os.getuid() == 0
        or str(os.getuid()) != environ.get("TRADEGRAD_UID")
        or str(os.getgid()) != environ.get("TRADEGRAD_GID")
    ):
        raise ValueError(
            "The existing non-root host UID/GID must match every trusted container"
        )


def validate_build(environ=os.environ) -> None:
    if not re.fullmatch(
        r"docker(?:\.io/library/docker)?@sha256:[0-9a-f]{64}",
        environ.get("DOCKER_CLI_IMAGE", ""),
    ):
        raise ValueError("Require an official docker CLI image repository digest")
    from packages.tradegrad_adapter.environment import environment_digest
    from packages.tradegrad_adapter.portable_environment import (
        portable_environment_digest,
    )

    for name, expected in (
        ("NATIVE_ENVIRONMENT_DIGEST", environment_digest()),
        ("PORTABLE_ENVIRONMENT_DIGEST", portable_environment_digest()),
    ):
        if environ.get(name) != expected:
            raise ValueError(
                "Evaluator environment identity must match this exact source tree"
            )


def prepare() -> None:
    """Explicit host-only step after the operator provisions a durable boundary."""
    validate_identity()
    if any(
        os.environ.get(name)
        for name in (
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        )
    ):
        raise ValueError(
            "Remove inherited remote/context/TLS overrides; select the explicit local socket"
        )
    validate_build()
    root = state_root()
    validate_owner(root)
    from backend.app.core.atomic_storage import provision_directory

    for child in (root / "storage", root / "runner"):
        provision_directory(child, durable_parent=root)
        validate_owner(child)
    socket = Path(os.environ["TRADEGRAD_DOCKER_SOCKET"])
    value = socket.stat()
    if (
        not stat.S_ISSOCK(value.st_mode)
        or str(value.st_gid) != os.environ.get("TRADEGRAD_DOCKER_GID")
        or not os.access(socket, os.R_OK | os.W_OK)
    ):
        raise ValueError(
            "Require existing authorized access to the specified local Docker socket"
        )
    print(
        "Durable directories and existing host identity validated; no Docker call performed"
    )


def validate_application() -> None:
    validate_identity()
    root = state_root()
    # Only leaf directories are bind-mounted. Their parent need not be mounted.
    storage = root / "storage"
    if Path(os.environ["STORAGE_ROOT"]) != storage:
        raise ValueError("Storage must use the identical absolute host/container path")
    validate_owner(storage)
    uri = urlsplit(os.environ["DATABASE_URL"])
    if (
        uri.scheme not in {"postgresql", "postgresql+psycopg"}
        or uri.hostname != "database"
        or uri.port not in {None, 5432}
        or uri.path != "/tradegrad"
        or uri.username != "tradegrad"
        or not uri.password
    ):
        raise ValueError(
            "This Compose profile requires its private database service URI"
        )


def validate_supervisor() -> None:
    root = state_root()
    if Path(os.environ["TRADEGRAD_RUNNER_STATE_DIR"]) != root / "runner":
        raise ValueError("Runner state must use the identical durable host path")
    validate_owner(root / "runner")
    from packages.tradegrad_adapter.environment import environment_digest
    from packages.tradegrad_adapter.portable_environment import (
        portable_environment_digest,
    )
    from runtimes.evaluator.portable_runner import PortableDockerEvaluator
    from runtimes.evaluator.runner import DockerEvaluator, IsolationUnavailable

    runner = DockerEvaluator()
    runner._preflight()
    info = json.loads(runner._command("info", "--format", "{{json .}}").stdout)
    if any(
        "rootless" in option or "userns" in option
        for option in info.get("SecurityOptions", [])
        if isinstance(option, str)
    ):
        raise IsolationUnavailable(
            "Remapped/rootless daemons need a separately reviewed ownership profile"
        )
    runner.bind_environment(environment_digest())
    PortableDockerEvaluator().bind_environment(portable_environment_digest())


def health(role: str) -> None:
    if role == "api":
        with urlopen(
            "http://127.0.0.1:8000/api/v1/health/ready", timeout=5
        ) as response:
            if response.status != 200:
                raise ValueError("API unavailable")
        return
    if role not in HEARTBEATS:
        raise ValueError("Unknown health role")
    from datetime import timedelta

    from sqlalchemy import select

    from backend.app.core.config import get_settings
    from backend.app.core.db import create_db_engine, session_factory
    from backend.app.models import WorkerHeartbeat, utcnow

    factory = session_factory(create_db_engine(get_settings()))
    with factory() as session:
        fresh = session.scalar(
            select(WorkerHeartbeat.worker_id)
            .where(
                WorkerHeartbeat.role == role,
                WorkerHeartbeat.heartbeat_at > utcnow() - timedelta(seconds=30),
            )
            .limit(1)
        )
        if fresh is None:
            raise ValueError("Worker role heartbeat unavailable")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("prepare", "run", "health", "migrate", "bootstrap")
    )
    parser.add_argument("role", nargs="?")
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            prepare()
            return
        if args.action == "health":
            health(args.role)
            return
        validate_application()
        if args.action == "run":
            if args.role not in ROLES:
                raise ValueError("Unknown role")
            if args.role in SUPERVISORS:
                validate_supervisor()
            command = ROLES[args.role]
        elif args.action == "migrate":
            command = ["alembic", "-c", "backend/alembic.ini", "upgrade", "head"]
        else:
            if not os.environ.get("BOOTSTRAP_EMAIL") or not os.environ.get(
                "BOOTSTRAP_PASSWORD"
            ):
                raise ValueError("Explicit bootstrap identity required")
            command = [
                "python",
                "-m",
                "backend.app.bootstrap",
                "--email",
                os.environ["BOOTSTRAP_EMAIL"],
            ]
            if os.environ.get("BOOTSTRAP_SUPERUSER") == "1":
                command.append("--superuser")
            if os.environ.get("BOOTSTRAP_SYNTHETIC") == "1":
                command.append("--with-synthetic-fixtures")
            if os.environ.get("BOOTSTRAP_E2E") == "1":
                if os.environ.get("BOOTSTRAP_SYNTHETIC") != "1":
                    raise ValueError(
                        "E2E probes require explicit synthetic provisioning"
                    )
                command.append("--with-e2e-fixtures")
        os.execvp(command[0], command)
    except Exception:  # noqa: BLE001 - fail closed without exposing private configuration
        # No SQL/URL/secrets, paths, subprocess stdout, or raw exceptions in logs.
        print(
            "Compose role preflight/health failed; check the private configuration and documented prerequisites",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
