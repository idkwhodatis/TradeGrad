"""Persist exact reviewed build-input and lock evidence before execution."""

import hashlib
from uuid import UUID, uuid4

from backend.app.core.atomic_storage import publish_bytes
from backend.app.models import Artifact
from backend.app.services import fenced_attempt
from packages.tradegrad_adapter.strategy_package import (
    canonical,
    capture_environment_evidence,
)


def publish_native_environment(
    *,
    factory,
    storage_root,
    attempt_root,
    run_id,
    attempt_id,
    generation,
    token,
    expected_environment_digest,
    image_id,
):
    evidence = capture_environment_evidence(expected_environment_digest, image_id)
    content = canonical(evidence)
    if len(content) > 2 * 1024 * 1024:
        raise ValueError("Execution environment evidence exceeds its byte bound")
    digest = hashlib.sha256(content).hexdigest()
    path = attempt_root / "execution-environment.json"
    publish_bytes(path, content, sha256=digest, durable_root=storage_root)
    with factory.begin() as session:
        fenced_attempt(session, attempt_id, generation, token)
        session.add(
            Artifact(
                id=uuid4(),
                run_id=UUID(str(run_id)),
                attempt_id=attempt_id,
                name="execution-environment.json",
                media_type="application/json",
                storage_key=str(path.relative_to(storage_root)),
                sha256=digest,
                size_bytes=len(content),
            )
        )
    return evidence
