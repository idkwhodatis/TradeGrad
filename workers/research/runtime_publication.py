"""Hash-bound replay observation publication in the existing fenced transaction."""

from pathlib import Path

from backend.app.core.atomic_storage import publish_bytes
from packages.tradegrad_adapter.runtime_observation import (
    MAX_OBSERVATION_BYTES,
    VERSION,
    _strict,
    canonical,
    digest,
    validate_runtime_observation,
)


def prepare_runtime_artifacts(*, config, result, records, root, settings=None):
    """Prepare one immutable sidecar; caller registers it before leaf completion.

    Historical/mock results without the supervisor envelope remain unavailable.
    Present-but-invalid evidence fails publication, never becomes absence.
    """
    if result.get("status") != "succeeded" or "runtime_observation" not in result:
        return []
    metadata = result["runtime_observation"]
    if (
        not isinstance(metadata, dict)
        or set(metadata) != {"path", "sha256", "bytes", "schema_version"}
        or metadata["path"] != "runtime-observation.json"
        or metadata["schema_version"] != VERSION
        or type(metadata["bytes"]) is not int
        or not 0 < metadata["bytes"] <= MAX_OBSERVATION_BYTES
    ):
        raise ValueError("Invalid runtime observation publication envelope")
    path = Path(root) / "output" / "runtime-observation.json"
    if (
        any(p.is_symlink() for p in (path, *path.parents))
        or not path.is_file()
        or path.stat().st_size != metadata["bytes"]
    ):
        raise ValueError("Missing or unsafe runtime observation artifact")
    data = path.read_bytes()
    if settings is not None and len(data) > getattr(
        settings, "max_artifact_bytes", MAX_OBSERVATION_BYTES
    ):
        raise ValueError("Runtime observation exceeds configured artifact budget")
    value = _strict(data)
    if canonical(value) != data or digest(value) != metadata["sha256"]:
        raise ValueError("Runtime observation hash or canonical bytes mismatch")
    bundle = result["bundle"]
    validate_runtime_observation(
        value,
        expected_identity=bundle["identity"],
        expected_runtime_kind="portable"
        if config.schema_version.startswith("tradegrad.portable.")
        else "native",
    )
    expected_name = (
        "portable-result.json"
        if config.schema_version.startswith("tradegrad.portable.")
        else "result-bundle.json"
    )
    economic = [item for item in records if item["name"] == expected_name]
    if len(economic) != 1:
        raise ValueError("Runtime observation requires one economic result artifact")
    window = value["initialization"]["window"]
    if any(window.get(key) != bundle["window"].get(key) for key in ("start", "end")):
        raise ValueError("Runtime observation reconstruction window mismatch")
    if value["runtime_kind"] == "native" and window != bundle["window"]:
        raise ValueError("Native runtime observation warmup mismatch")
    restored = getattr(config, "rng_restore", None)
    if value["rng"]["restored_from_sha256"] != (
        None if restored is None else digest(restored)
    ):
        raise ValueError(
            "Runtime observation restored RNG differs from immutable configuration"
        )
    target = Path(root) / "artifacts" / "runtime-observation.json"
    # The economic artifact already provides the storage-root-relative address.
    # Infer the same root without accepting any child-supplied absolute path.
    storage_root = settings.storage_root if settings is not None else target
    if settings is None:
        key = Path(economic[0]["storage_key"])
        if key.is_absolute() or ".." in key.parts:
            raise ValueError("Runtime economic storage address must be relative")
        storage_parts = key.parts
        storage_root = Path(root) / "artifacts" / expected_name
        for _ in storage_parts:
            storage_root = storage_root.parent
        if (storage_root / key).absolute() != (
            Path(root) / "artifacts" / expected_name
        ).absolute():
            raise ValueError(
                "Runtime economic storage address differs from publication"
            )
    if not target.absolute().is_relative_to(Path(storage_root).absolute()):
        raise ValueError("Runtime publication outside durable storage")
    publish_bytes(
        target, data, sha256=metadata["sha256"], durable_root=Path(storage_root)
    )
    return [
        {
            "name": target.name,
            "media_type": "application/json",
            "storage_key": str(target.relative_to(storage_root)),
            "sha256": metadata["sha256"],
            "size_bytes": len(data),
        }
    ]


def attach_runtime_reference(outcome, artifact, name, result):
    if name == "runtime-observation.json":
        if "runtime_observation" not in result:
            raise ValueError("Runtime observation reference lacks supervisor metadata")
        outcome["runtime_observation"] = {
            "artifact_id": str(artifact.id),
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
        }
