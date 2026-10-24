"""Selected native code is inert text; only the final job's input window is staged."""

import hashlib
from pathlib import Path

from pydantic import TypeAdapter

from backend.app.storage import read_verified, safe_path
from packages.tradegrad_adapter.config import canonical_bytes
from packages.tradegrad_adapter.final_config import FinalEvaluationConfig
from packages.tradegrad_adapter.final_windows import stage_final_dataset_window
from packages.tradegrad_adapter.windows import DatasetWindowReport


def stage_final_input(
    *,
    config: FinalEvaluationConfig,
    manifest: dict,
    storage_root: Path,
    dataset_storage_key: str,
    code: str,
    target: Path,
    max_bytes: int,
) -> Path:
    config = FinalEvaluationConfig.model_validate(config.model_dump(mode="json"))
    raw = code.encode()
    if not raw or len(raw) > 1_048_576:
        raise ValueError("Selected strategy exceeds the bounded source contract")
    if hashlib.sha256(raw).hexdigest() != config.selected_strategy.sha256:
        raise ValueError("Selected strategy differs from the frozen final selection")
    staged = stage_final_dataset_window(
        config,
        manifest,
        lambda name: read_verified(
            storage_root,
            f"{dataset_storage_key}/{name}",
            manifest["files"][name],
            max_bytes,
        ),
    )
    source = config.source_config
    request = {
        "task": source.task,
        "market": source.market_profile.market,
        "profile": source.profile,
        "start_date": str(config.holdout.start),
        "end_date": str(config.holdout.end),
        "evaluation_config": {
            "engine_overrides": source.market_profile.engine_kwargs(),
            "window": {
                "start": str(config.holdout.start),
                "end": str(config.holdout.end),
                "role": "holdout",
            },
            "warmup": {
                "sessions": source.warmup.sessions,
                "history_policy": source.warmup.history_policy,
            },
            "benchmark": None,
            "run_config_sha256": config.sha256,
        },
    }
    if source.research_capture is not None:
        request["evaluation_config"]["research_capture"] = (
            source.research_capture.model_dump(mode="json")
        )
    if source.strategy_contract is not None:
        request["evaluation_config"]["strategy_contract"] = (
            source.strategy_contract.model_dump(mode="json")
        )
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
    # No selection, optimizer checkpoint, model response or other window mounts.
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
