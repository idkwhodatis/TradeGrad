"""One-shot original-start staging. Observation dates never choose engine inputs."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import TypeAdapter

from backend.app.storage import read_verified, safe_path
from packages.strategy_sdk import canonical_json, hash_json, parse_manifest
from packages.tradegrad_adapter.config import canonical_bytes
from packages.tradegrad_adapter.independent_config import (
    NATIVE_SCHEMA,
    parse_independent_config,
)
from packages.tradegrad_adapter.portable_windows import _stage_portable_window
from packages.tradegrad_adapter.windows import (
    DEFAULT_LIMITS,
    DatasetWindowReport,
    _stage_resolved_dataset_window,
)
from workers.research.portable_staging import (
    _write_portable_inputs,
    validate_portable_binding,
)


def validate_portable_independent_binding(config, manifest, parameters):
    config = parse_independent_config(config.model_dump(mode="json"))
    parsed = parse_manifest(canonical_json(manifest))
    if (
        hash_json(parsed) != config.selected_manifest.sha256
        or hash_json(parameters) != config.selected_parameters.sha256
        or parsed.code_sha256 != config.selected_strategy.sha256
    ):
        raise ValueError("Independent portable selected metadata differs")
    validate_portable_binding(config.effective_source(), parsed, parameters)
    return parsed


def _stage(config, manifest, read):
    effective = config.effective_source()
    if config.schema_version == NATIVE_SCHEMA:
        return _stage_resolved_dataset_window(
            effective,
            config.split,
            config.reconstruction_window,
            manifest,
            read,
            limits=DEFAULT_LIMITS,
        )
    return _stage_portable_window(
        effective, config.split, manifest, read, window=config.reconstruction_window
    )


def _reader(storage_root, key, manifest, max_bytes):
    hashes = manifest.get("files", {}) | manifest.get("artifact_files", {})

    def read(name):
        if name not in hashes:
            raise ValueError("Independent input is absent from immutable dataset")
        return read_verified(storage_root, f"{key}/{name}", hashes[name], max_bytes)

    return read


def preflight_independent_window(config, dataset, settings):
    return _stage(
        config,
        dataset.manifest,
        _reader(
            settings.storage_root,
            dataset.storage_key,
            dataset.manifest,
            settings.max_artifact_bytes,
        ),
    ).report


def stage_independent_input(
    *,
    config,
    manifest,
    storage_root,
    dataset_storage_key,
    code,
    target,
    max_bytes,
    policy_manifest=None,
    parameters=None,
):
    config = parse_independent_config(config.model_dump(mode="json"))
    raw = code.encode("utf-8")
    if (
        not raw
        or len(raw) > 900_000
        or hashlib.sha256(raw).hexdigest() != config.selected_strategy.sha256
    ):
        raise ValueError("Independent exact source bytes differ")
    staged = _stage(
        config,
        manifest,
        _reader(storage_root, dataset_storage_key, manifest, max_bytes),
    )
    source = config.effective_source()
    if config.schema_version != NATIVE_SCHEMA:
        selected = validate_portable_independent_binding(
            config, policy_manifest, parameters
        )
        return _write_portable_inputs(
            config=source,
            staged=staged,
            seed_manifest=selected,
            parameters=parameters,
            code=code,
            target=target,
            run_config_sha256=config.sha256,
            replay_rng_state=config.rng_restore,
        )
    request = {
        "task": source.task,
        "market": source.market_profile.market,
        "profile": source.profile,
        "start_date": str(config.reconstruction_window.start),
        "end_date": str(config.reconstruction_window.end),
        "evaluation_config": {
            "engine_overrides": source.market_profile.engine_kwargs(),
            "window": {
                "start": str(config.reconstruction_window.start),
                "end": str(config.reconstruction_window.end),
                "role": config.split,
            },
            "warmup": {
                "sessions": source.warmup.sessions,
                "history_policy": source.warmup.history_policy,
            },
            "benchmark": None,
            "run_config_sha256": config.sha256,
            "result_policy": "independent_observation.v1",
            "research_capture": config.research_capture.model_dump(mode="json"),
        },
        "runtime_observation": {
            "schema_version": "tradegrad.runtime-observation-request.v1",
            "rng_restore": config.rng_restore,
        },
    }
    if config.selected_contract is not None:
        request["evaluation_config"]["strategy_contract"] = (
            config.selected_contract.model_dump(mode="json")
        )
    target = Path(target)
    if any(path.is_symlink() for path in (target, *target.parents)):
        raise ValueError("Independent input cannot traverse symlinks")
    target.mkdir(parents=True, mode=0o700)
    for name, content in {
        **staged.files,
        "strategy.py": raw,
        "request.json": canonical_bytes(request),
    }.items():
        path = safe_path(target, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(content)
    with (target.parent / "window-report.json").open("xb") as stream:
        stream.write(
            canonical_bytes(
                TypeAdapter(DatasetWindowReport).dump_python(staged.report, mode="json")
            )
        )
    for path in target.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    target.chmod(0o555)
    return target
