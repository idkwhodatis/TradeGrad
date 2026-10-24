"""Pinned browser CSV/source through the real native entrypoint, no pytest import.

The caller owns subprocess/transport admission. This is not DB or Docker proof.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

from packages.research_trace.contracts import CaptureConfig
from packages.research_trace.samples import CONTROLLED_SOURCE_SHA256
from packages.research_trace.templates import native_contract
from packages.tradegrad_adapter.config import DateWindow, MarketConfig
from packages.tradegrad_adapter.windows import stage_csv_window
from runtimes.evaluator.entrypoint import main


def run_case(options):
    root = Path(__file__).resolve().parents[2]
    source = root / "seed_strategies/research_time.py"
    assert not source.is_symlink()
    assert (
        hashlib.sha256(source.read_bytes()).hexdigest()
        == CONTROLLED_SOURCE_SHA256["daily_timing"]
    )
    raw = (root / "frontend/tests/fixtures/research-cn.csv").read_bytes()
    assert (
        hashlib.sha256(raw).hexdigest()
        == "2ad75d287f03a5186201c1d9a1d029dd15940c8d78649c8111ae38d1a2046a1b"
    )
    settings = json.loads(
        (root / "frontend/tests/fixtures/independent-parity-window.json").read_text()
    )
    if options["window"] == "original":
        settings["windows"]["train"]["end"] = "2024-03-15"
        settings["windows"]["validation"]["start"] = "2024-03-15"
    else:
        assert options["window"] == "paging"
    split = options.get("split", "train")
    assert split in {"train", "validation"}
    window = DateWindow.model_validate(settings["windows"][split])
    staged = stage_csv_window(
        raw,
        listing_id="target_symbol",
        window=window,
        warmup_sessions=settings["warmupSessions"],
        history_policy="require",
    )
    with tempfile.TemporaryDirectory() as directory:
        inputs, outputs = Path(directory) / "input", Path(directory) / "output"
        (inputs / "data").mkdir(parents=True)
        (inputs / "data/INDEX.csv").write_bytes(staged.content)
        (inputs / "strategy.py").write_bytes(source.read_bytes())
        profile = MarketConfig.for_market("CN").model_copy(
            update={"initial_cash": settings["initialCash"]}
        )
        config = {
            "engine_overrides": profile.engine_kwargs(),
            "window": {**window.model_dump(mode="json"), "role": split},
            "warmup": {
                "sessions": settings["warmupSessions"],
                "history_policy": "require",
            },
            "benchmark": None,
            "run_config_sha256": "a" * 64,
            "research_capture": CaptureConfig(mode=options["capture"]).model_dump(
                mode="json"
            ),
            "strategy_contract": native_contract("daily_timing").model_dump(
                mode="json"
            ),
        }
        if options["independent"]:
            config["result_policy"] = "independent_observation.v1"
        request = {
            "task": "cn_time",
            "market": "CN",
            "profile": "corrected_explicit_config",
            "start_date": window.start.isoformat(),
            "end_date": window.end.isoformat(),
            "evaluation_config": config,
        }
        if options.get("restore") is not None:
            request["runtime_observation"] = {
                "schema_version": "tradegrad.runtime-observation-request.v1",
                "rng_restore": options["restore"],
            }
        (inputs / "request.json").write_text(json.dumps(request))
        main(inputs, outputs)


if __name__ == "__main__":
    run_case(json.loads(sys.stdin.read()))
