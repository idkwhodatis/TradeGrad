"""Write two disjoint immutable portable inputs. Candidate source is inert bytes."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from backend.app.storage import read_verified, safe_path
from packages.strategy_sdk import (
    StateCodec,
    canonical_json,
    hash_json,
    parse_json,
    parse_manifest,
)
from packages.tradegrad_adapter.portable_config import PortableRunConfig
from packages.tradegrad_adapter.portable_windows import (
    PortableWindowReport,
    stage_portable_dataset_window,
)
from runtimes.evaluator.portable_runner import safe_policy_manifest


def validate_portable_binding(
    config: PortableRunConfig, policy_manifest, parameters: Mapping
):
    """Verify frozen seed metadata without loading or executing policy Python."""
    config = PortableRunConfig.model_validate_json(config.canonical_bytes())
    manifest = parse_manifest(
        policy_manifest
        if isinstance(policy_manifest, (bytes, str))
        else canonical_json(policy_manifest)
    )
    params = parse_json(canonical_json(parameters))
    if (
        type(params) is not dict
        or hash_json(params) != config.seed_parameters.sha256
        or manifest.parameters_sha256 != config.seed_parameters.sha256
    ):
        raise ValueError("Portable immutable parameters changed")
    if (
        hash_json(manifest) != config.seed_manifest.sha256
        or manifest.code_sha256 != config.seed_strategy.sha256
    ):
        raise ValueError("Portable immutable seed manifest or code reference changed")
    if (
        manifest.strategy_kind,
        manifest.market,
        manifest.engine_profile.profile_sha256,
        manifest.engine_profile.environment_sha256,
    ) != (
        config.strategy_kind,
        config.market,
        config.engine_profile.sha256,
        config.environment_digest,
    ):
        raise ValueError("Portable seed metadata differs from frozen run semantics")
    if config.warmup.sessions < max(
        manifest.data.lookback_bars, manifest.data.warmup_bars
    ):
        raise ValueError(
            "Portable warmup must cover the manifest's full declared lookback"
        )
    if manifest.universe.semantics != "user_fixed":
        raise ValueError("Initial portable research requires an explicit fixed basket")
    StateCodec(manifest.state_schema).decode(config.initial_state)
    if manifest.authoring is not None:
        StateCodec(manifest.authoring.parameter_schema).encode(params)
        return manifest
    # The original reviewed reference templates retain exact parameter restrictions.
    name = "quantity" if manifest.strategy_kind == "event" else "top_n"
    upper = 1_000_000 if name == "quantity" else 100
    if (
        set(params) != {name}
        or type(params[name]) is not int
        or not 1 <= params[name] <= upper
    ):
        raise ValueError("Unsupported portable reference business parameters")
    if name == "quantity" and params[name] % (100 if config.market == "CN" else 1):
        raise ValueError(
            "Portable event quantity must respect the reviewed market lot size"
        )
    return manifest


@dataclass(frozen=True)
class PortableStagedInput:
    engine_root: Path
    policy_root: Path
    manifest: object
    request: Mapping
    report: PortableWindowReport


def stage_portable_input(
    *,
    config: PortableRunConfig,
    manifest: dict,
    storage_root: Path,
    dataset_storage_key: str,
    code: str,
    policy_manifest,
    parameters: Mapping,
    target: Path,
    max_bytes: int,
    split: str | None = None,
    evaluation_window=None,
    window: dict | None = None,
) -> PortableStagedInput:
    config = PortableRunConfig.model_validate_json(config.canonical_bytes())
    seed_manifest = validate_portable_binding(config, policy_manifest, parameters)
    raw = code.encode("utf-8")
    # Policy mount total has a 1 MiB cap including its other inputs.
    if not raw or len(raw) > 900_000:
        raise ValueError("Portable candidate source exceeds bounded text contract")
    if window is not None:
        if evaluation_window is not None or (
            split is not None and split != window.get("split")
        ):
            raise ValueError("Conflicting portable evaluation window")
        evaluation_window, split = window, window.get("split")
    if split is None:
        raise ValueError("Portable staging requires an explicit research split")
    hashes = manifest.get("files", {}) | manifest.get("artifact_files", {})

    def read(name):
        if name not in hashes:
            raise ValueError("Portable artifact absent from immutable dataset")
        return read_verified(
            storage_root, f"{dataset_storage_key}/{name}", hashes[name], max_bytes
        )

    staged = stage_portable_dataset_window(
        config, split, manifest, read, evaluation_window=evaluation_window
    )
    return _write_portable_inputs(
        config=config,
        staged=staged,
        seed_manifest=seed_manifest,
        parameters=parameters,
        code=code,
        target=target,
        run_config_sha256=config.sha256,
    )


def _write_portable_inputs(
    *,
    config,
    staged,
    seed_manifest,
    parameters,
    code,
    target,
    run_config_sha256,
) -> PortableStagedInput:
    """Private serialization only; callers already validate execution authority.

    Research always supplies its own config hash. The separate frozen-final
    entry point supplies the exact final config hash after frozen selection
    verification. This helper never chooses a window or reads market data.
    """
    raw = code.encode("utf-8")
    if not raw or len(raw) > 900_000:
        raise ValueError("Portable candidate source exceeds bounded text contract")
    if staged.report.listing_count > seed_manifest.data.max_listings:
        raise ValueError("Dataset exceeds policy manifest listing bound")
    value = seed_manifest.model_dump(mode="json")
    value["code_sha256"] = hashlib.sha256(raw).hexdigest()
    value["universe"]["snapshot_sha256"] = staged.report.universe_sha256
    value["prices"]["actions_snapshot_sha256"] = staged.report.actions_sha256
    value["reference_result_sha256"] = None
    candidate_manifest = parse_manifest(canonical_json(value))
    request = {
        "schema_version": "portable-engine-request.v1",
        "manifest": candidate_manifest.model_dump(mode="json"),
        "window": staged.report.window.model_dump(mode="json"),
        "initial_cash": config.initial_cash,
        "initial_state": config.initial_state.model_dump(mode="json"),
        "input_identity": {
            "run_config_sha256": run_config_sha256,
            "market_data_sha256": staged.report.market_data_sha256,
        },
    }
    target = Path(target)
    if any(path.is_symlink() for path in (target, *target.parents)):
        raise ValueError("Portable input path cannot traverse a symlink")
    # Existing/partially written attempts are never reused.
    target.mkdir(parents=True, mode=0o700)
    engine, policy = target / "engine", target / "policy"
    engine.mkdir(mode=0o700)
    policy.mkdir(mode=0o700)
    for root, files in (
        (
            engine,
            {"request.json": canonical_json(request), "market.json": staged.content},
        ),
        (
            policy,
            {
                "policy.py": raw,
                "parameters.json": canonical_json(parameters),
                "manifest.json": canonical_json(
                    safe_policy_manifest(candidate_manifest)
                ),
            },
        ),
    ):
        if root == policy and sum(map(len, files.values())) > 1024 * 1024:
            raise ValueError("Portable policy inputs exceed mount budget")
        for name, content in files.items():
            path = safe_path(root, name)
            with path.open("xb") as output:
                output.write(content)
            path.chmod(0o444)
        root.chmod(0o555)
    target.chmod(0o555)
    return PortableStagedInput(
        engine, policy, candidate_manifest, request, staged.report
    )
