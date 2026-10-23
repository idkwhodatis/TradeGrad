"""Reviewed portable build-input identity; actual image IDs are pinned separately."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Both container build closures, trusted coordinator/config/staging and source
# locks are pinned. Dynamic discovery includes every SDK/runtime Python file,
# never caches, secrets, candidate artifacts or untracked deployment data.
TREES = (
    "packages/strategy_sdk",
    "runtimes/portable_engine",
    "runtimes/portable_policy",
)
INPUTS = (
    "workers/research/containment.py",
    "backend/app/core/atomic_storage.py",
    "backend/app/services.py",
    "backend/app/experiment_services.py",
    ".dockerignore",
    "runtimes/portable_engine/Dockerfile",
    "runtimes/portable_policy/Dockerfile",
    "runtimes/evaluator/Dockerfile",
    "runtimes/evaluator/__init__.py",
    "runtimes/evaluator/runner.py",
    "runtimes/evaluator/portable_runner.py",
    "runtimes/evaluator/requirements.lock",
    "runtimes/portable_engine/calendar-requirements.lock",
    "runtimes/portable_policy/requirements.lock",
    "runtimes/research/requirements.lock",
    "runtimes/data/requirements.lock",
    "backend/requirements.lock",
    "packages/tradegrad_adapter/config.py",
    "packages/tradegrad_adapter/benchmarks.py",
    "backend/app/benchmark_services.py",
    "packages/tradegrad_adapter/native_contract.py",
    "packages/tradegrad_adapter/windows.py",
    "packages/tradegrad_adapter/portable_config.py",
    "packages/tradegrad_adapter/portable_windows.py",
    "packages/tradegrad_adapter/portable_final_config.py",
    "packages/tradegrad_adapter/portable_environment.py",
    "packages/tradegrad_adapter/portable_results.py",
    "packages/tradegrad_adapter/portable_prompts.py",
    "packages/tradegrad_adapter/optimizer.py",
    "packages/tradegrad_adapter/model_calls.py",
    "packages/tradegrad_adapter/model_profiles.py",
    "packages/tradegrad_adapter/model_sdk_process.py",
    "workers/research/model_provider.py",
    "packages/tradegrad_adapter/checkpoint.py",
    "packages/marketdata/calendars.py",
    "packages/marketdata/storage.py",
    "packages/marketdata/portable_fixture.py",
    "packages/marketdata/contracts.py",
    "packages/marketdata/normalize.py",
    "packages/marketdata/venues.py",
    "packages/marketdata/catalog.py",
    "packages/marketdata/providers/registry.py",
    "workers/data/executor.py",
    "backend/app/data_schemas.py",
    "backend/app/data_services.py",
    "workers/research/portable_staging.py",
    "workers/research/portable_final_staging.py",
    "workers/research/portable_final_evaluation.py",
    "workers/research/portable_optimization.py",
    "workers/research/executor.py",
    "workers/research/lease.py",
    "backend/app/portable_services.py",
    "backend/app/portable_schemas.py",
    "backend/app/portable_final_services.py",
    "backend/app/portable_final_schemas.py",
    "packages/tradegrad_adapter/strategy_package.py",
    "backend/app/strategy_package_services.py",
    "backend/app/research_services.py",
    "backend/app/models.py",
    "backend/app/storage.py",
    "backend/app/core/config.py",
    "backend/app/model_containment.py",
    "src/optimizer.py",
    "src/prompt.py",
    "src/util.py",
)


def portable_environment_manifest(root: Path = ROOT) -> dict:
    root = Path(root)
    names = set(INPUTS)
    for name in TREES:
        directory = root / name
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError("Reviewed portable runtime tree missing or symlinked")
        names.update(str(p.relative_to(root)) for p in directory.rglob("*.py"))
    files = {}
    for name in sorted(names):
        path = root / name
        if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
            raise ValueError(f"Reviewed portable input missing or symlinked: {name}")
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"schema_version": "tradegrad.portable-build-inputs.v1", "files": files}


def portable_environment_digest(root: Path = ROOT) -> str:
    return hashlib.sha256(
        json.dumps(
            portable_environment_manifest(root), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


# Explicit aliases allow reuse of generic environment-evidence tooling, without
# ever substituting this digest into native configuration or image validation.
environment_manifest = portable_environment_manifest
environment_digest = portable_environment_digest

if __name__ == "__main__":
    print(portable_environment_digest())
