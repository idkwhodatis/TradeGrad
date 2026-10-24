"""Content identity of reviewed research/evaluator inputs, not the image ID.

The actual Docker image ID is pinned separately on first research preparation.
Image labels bind this build-input digest to that image; metadata alone is not
proof of an independently trusted native ledger or reproducible image build.
"""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INPUTS = (
    ".dockerignore",
    "workers/research/containment.py",
    "workers/research/trace_publication.py",
    "backend/app/core/atomic_storage.py",
    "src/__init__.py",
    "src/loss.py",
    "src/evaluate.py",
    "src/evaluate_time.py",
    "src/market.py",
    "src/data_cache.py",
    "src/resources.py",
    "src/optimizer.py",
    "src/prompt.py",
    "src/util.py",
    "src/llm_client.py",
    "packages/tradegrad_adapter/config.py",
    "packages/tradegrad_adapter/benchmarks.py",
    "backend/app/benchmark_services.py",
    "packages/tradegrad_adapter/native_contract.py",
    "packages/tradegrad_adapter/windows.py",
    "packages/tradegrad_adapter/optimizer.py",
    "packages/tradegrad_adapter/checkpoint.py",
    "packages/tradegrad_adapter/model_calls.py",
    "packages/tradegrad_adapter/model_profiles.py",
    "packages/tradegrad_adapter/model_sdk_process.py",
    "workers/research/model_provider.py",
    "packages/research_trace/__init__.py",
    "packages/research_trace/contracts.py",
    "packages/research_trace/binding.py",
    "packages/research_trace/storage.py",
    "packages/research_trace/recorder.py",
    "packages/research_trace/native.py",
    "packages/research_trace/portable.py",
    "packages/research_trace/transport.py",
    "packages/research_trace/comparison_contracts.py",
    "packages/research_trace/comparison_economics.py",
    "packages/research_trace/comparison_semantics.py",
    "packages/research_trace/comparison_storage.py",
    "packages/research_trace/diagnostic_parity.py",
    "packages/research_trace/diagnostic_parity_contracts.py",
    "packages/research_trace/templates.py",
    "seed_strategies/research_time.py",
    "seed_strategies/research_cross.py",
    "packages/tradegrad_adapter/portable_prompts.py",
    "packages/strategy_sdk/templates/event_trend.py",
    "packages/strategy_sdk/templates/portfolio_momentum.py",
    "packages/tradegrad_adapter/results.py",
    "packages/tradegrad_adapter/independent_results.py",
    "packages/tradegrad_adapter/runtime_observation.py",
    "workers/research/runtime_publication.py",
    "workers/research/diagnostic_parity_publication.py",
    "packages/tradegrad_adapter/independent_config.py",
    "workers/research/independent_staging.py",
    "workers/research/independent_evaluation.py",
    "backend/app/independent_services.py",
    "backend/app/comparison_services.py",
    "packages/tradegrad_adapter/indicators.py",
    "packages/tradegrad_adapter/environment.py",
    "runtimes/evaluator/requirements.lock",
    "runtimes/research/requirements.lock",
    "packages/tradegrad_adapter/final_config.py",
    "packages/tradegrad_adapter/final_windows.py",
    "packages/tradegrad_adapter/strategy_package.py",
    "workers/research/environment_evidence.py",
    "workers/research/final_staging.py",
    "workers/research/final_evaluation.py",
    "workers/research/lease.py",
    "backend/app/final_evaluation_services.py",
    "backend/app/experiment_services.py",
    "workers/research/optimization.py",
    "workers/research/executor.py",
    "backend/app/research_services.py",
    "backend/app/services.py",
    "backend/app/models.py",
    "backend/app/storage.py",
    "backend/app/core/config.py",
    "backend/app/model_containment.py",
    "runtimes/evaluator/runner.py",
    "runtimes/evaluator/entrypoint.py",
    "runtimes/evaluator/Dockerfile",
)


def environment_manifest(root: Path = ROOT) -> dict:
    files = {}
    for name in INPUTS:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Reviewed runtime input is missing or a symlink")
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"schema_version": "tradegrad.build-inputs.v1", "files": files}


def environment_digest(root: Path = ROOT) -> str:
    data = json.dumps(
        environment_manifest(root), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(data).hexdigest()


if __name__ == "__main__":
    print(environment_digest())
