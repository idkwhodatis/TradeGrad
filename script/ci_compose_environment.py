"""Generate masked, job-local Compose test identities, never operator credentials."""

import os
import secrets
from pathlib import Path

from packages.tradegrad_adapter.environment import environment_digest
from packages.tradegrad_adapter.portable_environment import portable_environment_digest


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true" or not os.environ.get("GITHUB_ENV"):
        raise SystemExit(
            "This helper only runs in an ephemeral hosted GitHub Actions job"
        )
    if os.getuid() == 0:
        raise SystemExit(
            "A non-root hosted job with existing Docker access is required"
        )
    suffix = secrets.token_hex(8)
    root = Path(os.environ["GITHUB_WORKSPACE"]) / "var" / f"compose-{suffix}"
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    socket = Path("/var/run/docker.sock")
    password, signing, first, second, admin = (secrets.token_hex(32) for _ in range(5))
    values = {
        "COMPOSE_PROJECT_NAME": f"tg-ci-{suffix}",
        "TRADEGRAD_RELEASE": f"ci-{os.environ['GITHUB_SHA'][:12]}-{suffix}",
        "TRADEGRAD_STATE_ROOT": str(root),
        "TRADEGRAD_UID": str(os.getuid()),
        "TRADEGRAD_GID": str(os.getgid()),
        "TRADEGRAD_DOCKER_SOCKET": str(socket.resolve(strict=True)),
        "TRADEGRAD_DOCKER_GID": str(socket.stat().st_gid),
        "POSTGRES_PASSWORD": password,
        "SECRET_KEY": signing,
        "DATABASE_URL": f"postgresql+psycopg://tradegrad:{password}@database:5432/tradegrad",
        "EVALUATOR_IMAGE": f"tradegrad-compose-native:ci-{suffix}",
        "PORTABLE_ENGINE_IMAGE": f"tradegrad-compose-engine:ci-{suffix}",
        "PORTABLE_POLICY_IMAGE": f"tradegrad-compose-policy:ci-{suffix}",
        "NATIVE_ENVIRONMENT_DIGEST": environment_digest(),
        "PORTABLE_ENVIRONMENT_DIGEST": portable_environment_digest(),
        "DATA_PROVIDER_MODE": "fixture",
        "MODEL_PROFILES_ENABLED": "false",
        "MODEL_PROFILES": "[]",
        "E2E_LIVE_PROVIDERS": "0",
        "E2E_MODEL_PROFILES": "0",
        "E2E_EMAIL": f"first-{suffix}@example.com",
        "E2E_OTHER_EMAIL": f"second-{suffix}@example.com",
        "E2E_PASSWORD": first,
        "E2E_OTHER_PASSWORD": second,
        "E2E_ADMIN_EMAIL": f"admin-{suffix}@example.com",
        "E2E_ADMIN_PASSWORD": admin,
        "TRADEGRAD_REQUIRE_SETTINGS_E2E": "1",
        "E2E_BASE_URL": "http://127.0.0.1:8080",
        "E2E_API_URL": "http://127.0.0.1:8080",
        "TRADEGRAD_HTTP_PORT": "8080",
    }
    for value in (password, signing, first, second, admin, values["DATABASE_URL"]):
        print(f"::add-mask::{value}", flush=True)
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as stream:
        for name, value in values.items():
            stream.write(f"{name}={value}\n")


if __name__ == "__main__":
    main()
