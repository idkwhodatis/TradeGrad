"""Hosted-only public image provenance, never resolved service secrets/config.

--resolve-bases pulls known official app/frontend bases and records immutable
repository digest references in GITHUB_ENV. Existing evaluator Dockerfiles keep
their reviewed Python tag; do not mislabel that as OCI-pinned build provenance.
"""

import argparse
import json
import os
import re
import time
from pathlib import Path

from script.compose_bounds import run_bounded

BASES = {
    "DOCKER_CLI_IMAGE": "docker:27-cli",
    "TRADEGRAD_PYTHON_IMAGE": "python:3.12.14-slim-bookworm",
    "TRADEGRAD_NODE_IMAGE": "node:24-bookworm-slim",
    "TRADEGRAD_NGINX_IMAGE": "nginx:stable-alpine",
}


def docker(*arguments):
    return run_bounded(
        [
            "docker",
            "--host",
            "unix://" + os.environ["TRADEGRAD_DOCKER_SOCKET"],
            *arguments,
        ],
        deadline=time.monotonic() + 600,
        max_stdout=262144,
        max_stderr=65536,
    )


def inspect(reference):
    rows = json.loads(docker("image", "inspect", reference))
    if len(rows) != 1 or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", rows[0].get("Id", "")
    ):
        raise ValueError("A unique immutable image ID is required")
    value = rows[0]
    labels = value.get("Config", {}).get("Labels") or {}
    return {
        "reference": reference,
        "image_id": value["Id"],
        "repository_digests": value.get("RepoDigests") or [],
        "os": value.get("Os"),
        "architecture": value.get("Architecture"),
        "environment_digest": labels.get("io.tradegrad.environment_digest"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolve-bases", action="store_true")
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("Only the explicit disposable hosted image gate is supported")
    if args.resolve_bases:
        values = {}
        for name, reference in BASES.items():
            docker("pull", "--quiet", reference)
            evidence = inspect(reference)
            repository = reference.split(":")[0]
            matching = [
                item
                for item in evidence["repository_digests"]
                if re.fullmatch(re.escape(repository) + r"@sha256:[0-9a-f]{64}", item)
            ]
            if len(matching) != 1:
                raise ValueError(
                    "Official base repository digest could not be established"
                )
            values[name] = matching[0]
        with Path(os.environ["GITHUB_ENV"]).open("a") as destination:
            for name, value in values.items():
                destination.write(f"{name}={value}\n")
        return
    release = os.environ["TRADEGRAD_RELEASE"]
    prefix = os.environ.get("TRADEGRAD_IMAGE_PREFIX", "tradegrad-local")
    references = [
        f"{prefix}/{role}:{release}" for role in ("api", "research", "data", "frontend")
    ]
    references += [
        os.environ[name]
        for name in (
            "EVALUATOR_IMAGE",
            "PORTABLE_ENGINE_IMAGE",
            "PORTABLE_POLICY_IMAGE",
            "DOCKER_CLI_IMAGE",
        )
    ]
    images = [inspect(reference) for reference in references]
    bases = {
        name: {"declared_reference": declared, "selected_reference": os.environ[name]}
        for name, declared in BASES.items()
    }
    output = {
        "schema_version": "tradegrad.compose-image-evidence.v1",
        "source_sha": os.environ["GITHUB_SHA"],
        "images": images,
        "application_bases": bases,
        "evaluator_base_declared_reference": "python:3.12.14-slim-bookworm",
        "evaluator_base_oci_digest_pinned": False,
    }
    target = Path("var/ci-evidence/compose-images.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x") as destination:
        json.dump(output, destination, indent=2, sort_keys=True)
        destination.write("\n")
    print(
        "Public image IDs/base references recorded; no resolved Compose secrets/config emitted"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - never expose raw image/config subprocess data
        raise SystemExit(
            "Hosted public image evidence could not be established"
        ) from None
