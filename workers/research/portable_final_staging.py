"""Strict frozen-final-only portable input path; no optimizer holdout admission."""

from __future__ import annotations

import hashlib
from pathlib import Path

from backend.app.storage import read_verified
from packages.strategy_sdk import canonical_json, hash_json, parse_manifest
from packages.tradegrad_adapter.portable_final_config import (
    PortableFinalEvaluationConfig,
)
from packages.tradegrad_adapter.portable_windows import _stage_portable_window
from workers.research.portable_staging import (
    _write_portable_inputs,
    validate_portable_binding,
)


def _validated(config):
    return PortableFinalEvaluationConfig.model_validate_json(config.canonical_bytes())


def validate_portable_final_binding(config, policy_manifest, parameters):
    """Verify a frozen candidate and the unchanged seed semantic contract."""
    config = _validated(config)
    manifest = parse_manifest(
        policy_manifest
        if isinstance(policy_manifest, (bytes, str))
        else canonical_json(policy_manifest)
    )
    if (
        hash_json(manifest) != config.selected_manifest.sha256
        or manifest.code_sha256 != config.selected_strategy.sha256
    ):
        raise ValueError("Selected portable manifest or source reference changed")
    if hash_json(parameters) != config.selected_parameters.sha256:
        raise ValueError("Selected portable parameters changed")
    # A frozen candidate can change only source business rules. Reconstruct the
    # original manifest's sole allowed delta, then validate its exact pinned hash.
    original = manifest.model_dump(mode="json")
    original["code_sha256"] = config.source_config.seed_strategy.sha256
    validate_portable_binding(config.source_config, original, parameters)
    return manifest


def stage_portable_final_dataset_window(config, manifest, read_file):
    """One exact frozen holdout plus past-only warmup, never an arbitrary window."""
    config = _validated(config)
    return _stage_portable_window(
        config.source_config,
        "holdout",
        manifest,
        read_file,
        window=config.holdout,
    )


def stage_portable_final_input(
    *,
    config: PortableFinalEvaluationConfig,
    manifest: dict,
    storage_root: Path,
    dataset_storage_key: str,
    code: str,
    policy_manifest,
    parameters,
    target: Path,
    max_bytes: int,
):
    config = _validated(config)
    selected = validate_portable_final_binding(config, policy_manifest, parameters)
    raw = code.encode("utf-8")
    if (
        not raw
        or len(raw) > 900_000
        or hashlib.sha256(raw).hexdigest() != config.selected_strategy.sha256
    ):
        raise ValueError("Selected portable source differs from frozen final selection")
    hashes = manifest.get("files", {}) | manifest.get("artifact_files", {})

    def read(name):
        if name not in hashes:
            raise ValueError("Portable artifact absent from immutable dataset")
        return read_verified(
            storage_root, f"{dataset_storage_key}/{name}", hashes[name], max_bytes
        )

    staged = stage_portable_final_dataset_window(config, manifest, read)
    return _write_portable_inputs(
        config=config.source_config,
        staged=staged,
        seed_manifest=selected,
        parameters=parameters,
        code=code,
        target=target,
        run_config_sha256=config.sha256,
    )
